"""Tests for EnrichmentService orchestration (fake fetcher + fake search, no network)."""

import pytest

from project_pilot.enrichment.fetch import FetchedPage
from project_pilot.enrichment.schemas import SearchResult
from project_pilot.enrichment.service import EnrichmentService
from project_pilot.errors import EnrichmentError, SourceBlockedError

_HOME = (
    '<html><body><nav><a href="/impressum">Impressum</a>'
    '<a href="/kontakt">Kontakt</a></nav></body></html>'
)
_IMPRESSUM = (
    "<html><body>Impressum. Geschäftsführer: Max Mustermann. "
    '<a href="mailto:bewerbung@muster-gmbh.de">Bewerbung</a> '
    '<a href="mailto:info@muster-gmbh.de">Info</a> '
    '<a href="tel:+49301234567">Anruf</a> Tel: 030 1234567</body></html>'
)


class _FakeFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> FetchedPage:
        self.fetched.append(url)
        if url not in self._pages:
            raise SourceBlockedError(f"no page for {url}")
        return FetchedPage(url=url, text=self._pages[url])

    async def aclose(self) -> None:
        return None


class _FakeSearch:
    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return self._results


def _pages() -> dict[str, str]:
    return {
        "https://www.muster-gmbh.de/": _HOME,
        "https://www.muster-gmbh.de/impressum": _IMPRESSUM,
        "https://www.muster-gmbh.de/kontakt": "<html><body>Kontaktformular</body></html>",
    }


async def test_enrich_finds_website_and_extracts_contacts() -> None:
    search = _FakeSearch(
        [
            SearchResult(url="https://de.linkedin.com/company/muster", title="LinkedIn"),
            SearchResult(url="https://www.muster-gmbh.de/impressum", title="Impressum"),
        ]
    )
    service = EnrichmentService(fetcher=_FakeFetcher(_pages()), search=search)

    result = await service.enrich(company="Muster GmbH", person="Max Mustermann")

    # the LinkedIn hit is skipped; the company origin is used as the homepage
    assert result.website == "https://www.muster-gmbh.de/"
    # person-matching addresses would rank first; here the role mailbox wins
    assert result.emails[0] == "bewerbung@muster-gmbh.de"
    assert "info@muster-gmbh.de" in result.emails
    assert "+49301234567" in result.phones
    assert result.persons == ["Max Mustermann"]
    assert "companies" in result.links.linkedin_company
    assert result.sources  # the pages actually read
    # a ready-to-copy connection note is always produced
    assert result.linkedin_message.startswith("Hallo Max,")
    assert "Muster GmbH" in result.linkedin_message


async def test_enrich_uses_known_url_and_skips_search() -> None:
    search = _FakeSearch([])
    service = EnrichmentService(fetcher=_FakeFetcher(_pages()), search=search)

    result = await service.enrich(
        company="Muster GmbH", known_url="https://www.muster-gmbh.de/impressum"
    )

    assert search.queries == []  # a known URL means no search call
    assert result.website == "https://www.muster-gmbh.de/"
    assert "bewerbung@muster-gmbh.de" in result.emails


async def test_enrich_without_any_subject_raises() -> None:
    service = EnrichmentService(fetcher=_FakeFetcher({}), search=_FakeSearch([]))
    with pytest.raises(EnrichmentError):
        await service.enrich(company=None, person=None, title=None)


async def test_enrich_returns_links_even_without_a_website() -> None:
    search = _FakeSearch([SearchResult(url="https://de.linkedin.com/x", title="only social")])
    service = EnrichmentService(fetcher=_FakeFetcher({}), search=search)

    result = await service.enrich(company="Nur Social GmbH")

    assert result.website is None
    assert result.emails == [] and result.phones == []
    assert result.links.linkedin_company  # research links always present
    assert result.linkedin_message  # connection message produced even without a website


async def test_enrich_survives_a_failing_contact_page() -> None:
    pages = {"https://www.muster-gmbh.de/": _HOME}  # /impressum + /kontakt missing → skipped
    service = EnrichmentService(
        fetcher=_FakeFetcher(pages),
        search=_FakeSearch([SearchResult(url="https://www.muster-gmbh.de/", title="Home")]),
    )

    result = await service.enrich(company="Muster GmbH")

    assert result.website == "https://www.muster-gmbh.de/"
    assert result.emails == []  # nothing crawlable, but no crash


async def test_enrich_signs_connection_message_with_sender() -> None:
    service = EnrichmentService(fetcher=_FakeFetcher({}), search=_FakeSearch([]), sender="Nik")
    result = await service.enrich(company="Muster GmbH", person="Max Mustermann")
    assert result.linkedin_message.rstrip().endswith("Nik")


async def test_enrich_honors_max_pages_budget() -> None:
    fetcher = _FakeFetcher(_pages())
    service = EnrichmentService(
        fetcher=fetcher,
        search=_FakeSearch([SearchResult(url="https://www.muster-gmbh.de/", title="Home")]),
        max_pages=1,
    )

    await service.enrich(company="Muster GmbH")

    assert fetcher.fetched == ["https://www.muster-gmbh.de/"]  # homepage only

"""Tests for EnrichmentService orchestration (fake fetcher + fake search, no network)."""

import pytest

from project_pilot.enrichment.fetch import FetchedPage
from project_pilot.enrichment.schemas import ContactDatum, SearchResult
from project_pilot.enrichment.service import EnrichmentService
from project_pilot.errors import EnrichmentError, SourceBlockedError


def _values(data: list[ContactDatum]) -> list[str]:
    return [datum.value for datum in data]


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
    assert _values(result.emails)[0] == "bewerbung@muster-gmbh.de"
    assert "info@muster-gmbh.de" in _values(result.emails)
    assert "+49301234567" in _values(result.phones)
    assert _values(result.persons) == ["Max Mustermann"]
    # nothing here came off a board page, so every find is marked as own research
    assert {datum.source for datum in result.emails} == {"web"}
    assert "companies" in result.links.linkedin_company
    assert result.sources  # the pages actually read
    # a ready-to-copy connection note is always produced
    assert result.linkedin_message.startswith("Guten Tag Max Mustermann,")
    assert "Muster GmbH" in result.linkedin_message


async def test_enrich_uses_known_url_and_skips_search() -> None:
    search = _FakeSearch([])
    service = EnrichmentService(fetcher=_FakeFetcher(_pages()), search=search)

    result = await service.enrich(
        company="Muster GmbH", known_url="https://www.muster-gmbh.de/impressum"
    )

    assert search.queries == []  # a known URL means no search call
    assert result.website == "https://www.muster-gmbh.de/"
    assert "bewerbung@muster-gmbh.de" in _values(result.emails)


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


_BOARD_PAGE_URL = "https://www.freelancermap.de/firma/556-muster-gmbh"
_BOARD_PAGE = (
    "<html><body><h1>Muster GmbH</h1>"
    '<a href="tel:+49301112233">030 1112233</a>'
    '<a href="mailto:s.koch@muster-gmbh.de">Mail</a>'
    '<a href="https://www.muster-gmbh.de/?utm_source=freelancermap">Website</a>'
    '<a href="https://www.linkedin.com/company/muster">LinkedIn</a>'
    "</body></html>"
)


async def test_the_board_company_page_is_read_first_and_marked_as_stated() -> None:
    # What the agent put on their own company page is a stated contact; what the
    # Impressum crawl turns up afterwards is our own find, and says so.
    search = _FakeSearch([])
    fetcher = _FakeFetcher({_BOARD_PAGE_URL: _BOARD_PAGE, **_pages()})
    service = EnrichmentService(fetcher=fetcher, search=search)

    result = await service.enrich(company="Muster GmbH", company_page=_BOARD_PAGE_URL)

    assert fetcher.fetched[0] == _BOARD_PAGE_URL  # before anything else
    assert result.company_page == _BOARD_PAGE_URL
    stated = {datum.value for datum in result.emails if datum.source == "freelancermap"}
    assert stated == {"s.koch@muster-gmbh.de"}
    assert "info@muster-gmbh.de" in _values(result.emails)
    assert result.emails[0].source == "freelancermap"  # the stated one leads
    assert any(datum.source == "freelancermap" for datum in result.phones)


async def test_the_company_pages_own_link_replaces_the_website_search() -> None:
    search = _FakeSearch([SearchResult(url="https://falsch.example/", title="wrong company")])
    fetcher = _FakeFetcher({_BOARD_PAGE_URL: _BOARD_PAGE, **_pages()})
    service = EnrichmentService(fetcher=fetcher, search=search)

    result = await service.enrich(company="Muster GmbH", company_page=_BOARD_PAGE_URL)

    assert search.queries == []  # the page said where the company lives
    assert result.website == "https://www.muster-gmbh.de/"


async def test_an_unreachable_company_page_costs_only_that_source() -> None:
    # A blocked or moved company page must not sink the lookup — the Impressum
    # crawl still runs and everything it finds is marked as our own research.
    search = _FakeSearch([SearchResult(url="https://www.muster-gmbh.de/", title="Home")])
    service = EnrichmentService(fetcher=_FakeFetcher(_pages()), search=search)

    result = await service.enrich(company="Muster GmbH", company_page=_BOARD_PAGE_URL)

    assert "bewerbung@muster-gmbh.de" in _values(result.emails)
    assert {datum.source for datum in result.emails} == {"web"}
    # The URL is still reported: it is where a human would look next.
    assert result.company_page == _BOARD_PAGE_URL


_RECORD_PAGE_URL = "https://www.freelancermap.de/firma/563-muster-digital-ag"
_RECORD_PAGE = (
    '<html><body><h1>Muster Digital AG</h1><script type="application/json">'
    '{"translations":{"email":"E-Mail","phone":"Telefon"},'
    '"company":{"phone":"089 123 45-0","email":"laura.muster@muster-gmbh.de",'
    '"website":"www.muster-gmbh.de"}}</script></body></html>'
)


async def test_a_board_page_that_only_ships_its_record_still_answers() -> None:
    # The real case behind this test: an agency page with no contact links at all,
    # whose e-mail, phone and website lived in embedded JSON. The lookup came back
    # empty and fell through to a search that found nothing either.
    search = _FakeSearch([SearchResult(url="https://falsch.example/", title="wrong company")])
    fetcher = _FakeFetcher({_RECORD_PAGE_URL: _RECORD_PAGE, **_pages()})
    service = EnrichmentService(fetcher=fetcher, search=search)

    result = await service.enrich(company="Muster Digital AG", company_page=_RECORD_PAGE_URL)

    assert search.queries == []  # the record said where the company lives
    assert result.website == "https://www.muster-gmbh.de/"
    assert result.emails[0] == ContactDatum("laura.muster@muster-gmbh.de", "freelancermap")
    assert result.phones[0] == ContactDatum("089 123 45-0", "freelancermap")

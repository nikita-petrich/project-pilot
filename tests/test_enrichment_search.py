"""Tests for the DuckDuckGo result parser and search providers (no live HTTP)."""

from pathlib import Path

from project_pilot.enrichment.fetch import FetchedPage
from project_pilot.enrichment.search import (
    DuckDuckGoSearch,
    NullSearchProvider,
    parse_ddg_results,
)

_DDG_HTML = (Path(__file__).parent / "fixtures" / "ddg_results.html").read_text(encoding="utf-8")


def test_parse_ddg_results_unwraps_redirects_and_direct_links() -> None:
    results = parse_ddg_results(_DDG_HTML, limit=10)
    urls = [result.url for result in results]
    assert urls == [
        "https://www.muster-gmbh.de/",
        "https://de.linkedin.com/company/muster-gmbh",
        "https://www.muster-gmbh.de/impressum",
    ]
    assert results[0].title.startswith("Muster GmbH")


def test_parse_ddg_results_respects_limit() -> None:
    assert len(parse_ddg_results(_DDG_HTML, limit=1)) == 1


class _FakeFetcher:
    def __init__(self, text: str) -> None:
        self.text = text
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        return FetchedPage(url=url, text=self.text)

    async def aclose(self) -> None:
        return None


async def test_duckduckgo_search_queries_endpoint_and_parses() -> None:
    fetcher = _FakeFetcher(_DDG_HTML)
    results = await DuckDuckGoSearch(fetcher).search("Muster GmbH", limit=2)
    assert len(results) == 2
    assert "html.duckduckgo.com" in fetcher.urls[0]
    assert "Muster" in fetcher.urls[0]


async def test_null_search_provider_finds_nothing() -> None:
    assert await NullSearchProvider().search("anything") == []


def test_parse_ddg_results_drops_non_http_redirect_targets() -> None:
    html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=javascript%3Aalert(1)">Bad</a>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ffirma.de%2F">Good</a>'
    )
    results = parse_ddg_results(html, limit=5)
    assert [result.url for result in results] == ["https://firma.de/"]

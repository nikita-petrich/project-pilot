"""Pluggable web search used to find a company's official website.

Only a company-website lookup is needed, so the provider surface is deliberately
tiny. The default reads DuckDuckGo's HTML endpoint (no API key); the result-parsing
is a pure function tested against a saved fixture, and the fetch goes through the
injected ``Fetcher`` so tests never hit the network. ``NullSearchProvider`` disables
search entirely (``ENRICHMENT_SEARCH=none``), leaving a supplied URL as the only way in.
"""

from typing import Protocol
from urllib.parse import parse_qs, quote_plus, urlsplit

from bs4 import BeautifulSoup

from project_pilot.enrichment.fetch import Fetcher
from project_pilot.enrichment.schemas import SearchResult

_DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"


class SearchProvider(Protocol):
    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]: ...


class NullSearchProvider:
    """A provider that finds nothing (search disabled)."""

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        return []


def _unwrap_ddg(href: str) -> str | None:
    """Resolve a DuckDuckGo result href to its real target URL (http/https only)."""
    if href.startswith("//"):
        href = f"https:{href}"
    parts = urlsplit(href)
    if "duckduckgo.com" in parts.netloc and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg")
        if not target:
            return None
        href = target[0]  # the unwrapped redirect target needs the same scheme check
        parts = urlsplit(href)
    return href if parts.scheme in ("http", "https") else None


def parse_ddg_results(html: str, *, limit: int) -> list[SearchResult]:
    """Extract result (url, title) pairs from a DuckDuckGo HTML results page."""
    soup = BeautifulSoup(html, "lxml")
    results: list[SearchResult] = []
    for anchor in soup.select("a.result__a"):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        url = _unwrap_ddg(href)
        if url is None:
            continue
        results.append(SearchResult(url=url, title=anchor.get_text(" ", strip=True)))
        if len(results) >= limit:
            break
    return results


class DuckDuckGoSearch:
    """Reads DuckDuckGo's HTML endpoint through the polite fetcher."""

    def __init__(self, fetcher: Fetcher, *, endpoint: str = _DDG_HTML_ENDPOINT) -> None:
        self._fetcher = fetcher
        self._endpoint = endpoint

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        page = await self._fetcher.fetch(f"{self._endpoint}?q={quote_plus(query)}&kl=de-de")
        return parse_ddg_results(page.text, limit=limit)

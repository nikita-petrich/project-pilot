"""Polite web fetcher for arbitrary company sites (UA, robots, delay, no 403 retry).

The freelancermap ``PolitenessClient`` is bound to one host; enrichment visits many,
so this is a small sibling with the same compliance posture: an identifying user
agent, a timeout, a spacing delay, a best-effort robots.txt gate per host, and a
hard stop (never a retry) on 403.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Protocol, Self
from urllib.parse import urlsplit

import httpx

from project_pilot.enrichment.robots import RobotsGate
from project_pilot.errors import EnrichmentError, SourceBlockedError

type Sleeper = Callable[[float], Awaitable[None]]

# Company pages are HTML; anything bigger is not contact data worth parsing.
_MAX_RESPONSE_BYTES = 2_000_000


def validate_target(url: str) -> None:
    """Refuse targets enrichment must never fetch: non-http(s) schemes and
    non-public IP literals (loopback, private, link-local) — the URLs come from
    web-search results and scraped pages, not from configuration.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EnrichmentError(f"unsupported URL scheme in {url!r}")
    host = parts.hostname or ""
    try:
        address = ip_address(host)
    except ValueError:
        return  # a hostname, not an IP literal
    if not address.is_global:
        raise EnrichmentError(f"refusing non-public address {host!r}")


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """A fetched page: its final URL (after redirects) and decoded text body."""

    url: str
    text: str


class Fetcher(Protocol):
    """The fetch surface enrichment needs (``WebFetcher`` and test fakes satisfy it)."""

    async def fetch(self, url: str) -> FetchedPage: ...
    async def aclose(self) -> None: ...


class WebFetcher:
    """Fetches pages across many hosts, politely and robots-aware."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 15.0,
        delay: float = 1.5,
        respect_robots: bool = True,
        sleeper: Sleeper = asyncio.sleep,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._delay = delay
        self._respect_robots = respect_robots
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True
        )
        self._robots = RobotsGate(user_agent, self._fetch_robots)
        self._delay_pending = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, url: str) -> FetchedPage:
        """Fetch ``url`` after the polite delay; raise ``SourceBlockedError`` if blocked.

        The robots check runs first so the sleep can honor the host's Crawl-delay
        (never below the configured spacing delay).
        """
        validate_target(url)
        if self._respect_robots and not await self._robots.allowed(url):
            raise SourceBlockedError(f"robots.txt disallows {url}")
        if self._delay_pending:
            await self._sleeper(self._effective_delay(url))
        self._delay_pending = True
        response = await self._client.get(url)
        if response.status_code == 403:
            raise SourceBlockedError(f"HTTP 403 for {url}")
        response.raise_for_status()
        validate_target(str(response.url))  # a redirect must not pivot to a private target
        content_type = response.headers.get("content-type", "")
        if content_type and "html" not in content_type and not content_type.startswith("text/"):
            raise EnrichmentError(f"not a text page ({content_type}): {url}")
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise EnrichmentError(f"response too large ({len(response.content)} bytes): {url}")
        return FetchedPage(url=str(response.url), text=response.text)

    def _effective_delay(self, url: str) -> float:
        if not self._respect_robots:
            return self._delay
        return max(self._delay, self._robots.crawl_delay(url) or 0.0)

    async def _fetch_robots(self, robots_url: str) -> str | None:
        try:
            response = await self._client.get(robots_url)
        except httpx.HTTPError:
            return None  # unreachable robots.txt: fail open (best effort)
        return response.text if response.status_code < 400 else None

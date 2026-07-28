"""Polite web fetcher for arbitrary company sites (UA, robots, delay, no 403 retry).

The freelancermap ``PolitenessClient`` is bound to one host; enrichment visits many,
so this is a small sibling with the same compliance posture: an identifying user
agent, a timeout, a spacing delay, a best-effort robots.txt gate per host, and a
hard stop (never a retry) on 403.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, Self

import httpx

from project_pilot.enrichment.robots import RobotsGate
from project_pilot.errors import SourceBlockedError

type Sleeper = Callable[[float], Awaitable[None]]


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
        """Fetch ``url`` after the polite delay; raise ``SourceBlockedError`` if blocked."""
        if self._delay_pending:
            await self._sleeper(self._delay)
        self._delay_pending = True
        if self._respect_robots and not await self._robots.allowed(url):
            raise SourceBlockedError(f"robots.txt disallows {url}")
        response = await self._client.get(url)
        if response.status_code == 403:
            raise SourceBlockedError(f"HTTP 403 for {url}")
        response.raise_for_status()
        return FetchedPage(url=str(response.url), text=response.text)

    async def _fetch_robots(self, robots_url: str) -> str | None:
        try:
            response = await self._client.get(robots_url)
        except httpx.HTTPError:
            return None  # unreachable robots.txt: fail open (best effort)
        return response.text if response.status_code < 400 else None

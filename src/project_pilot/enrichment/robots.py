"""Shared, best-effort robots.txt gate used by every enrichment fetcher.

One instance caches a parsed ``robots.txt`` per host. The robots file itself is
fetched through an injected async getter (plain text, never rendered), so both the
httpx and the Playwright fetcher reuse the exact same allow/deny logic. Unreachable
robots means "fail open" — the same lenient stance the ingestion client takes.
"""

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

type RobotsGetter = Callable[[str], Awaitable[str | None]]


class RobotsGate:
    """Caches per-host robots rules and answers ``allowed(url)`` for one user agent."""

    def __init__(self, user_agent: str, getter: RobotsGetter) -> None:
        self._user_agent = user_agent
        self._getter = getter
        self._cache: dict[str, RobotFileParser] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.netloc
        parser = self._cache.get(host)
        if parser is None:
            text = await self._getter(f"{parts.scheme}://{host}/robots.txt")
            parser = RobotFileParser()
            parser.parse((text or "").splitlines())
            self._cache[host] = parser
        return parser.can_fetch(self._user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        """The cached host's Crawl-delay for this agent (None before ``allowed`` ran)."""
        parser = self._cache.get(urlsplit(url).netloc)
        if parser is None:
            return None
        delay = parser.crawl_delay(self._user_agent)
        return float(delay) if delay is not None else None

"""Polite web fetcher for arbitrary company sites (UA, robots, delay, no 403 retry).

The freelancermap ``PolitenessClient`` is bound to one host; enrichment visits many,
so this is a small sibling with the same compliance posture: an identifying user
agent, a timeout, a spacing delay, a best-effort robots.txt gate per host, and a
hard stop (never a retry) on 403.

Because the URLs come from web-search results for a scraped company name (untrusted
input), the fetcher also guards against SSRF: it resolves each host and refuses any
target that is — or resolves to — a non-public address, follows redirects manually
so a redirect cannot pivot onto a private target unchecked, and streams the body
under a byte budget so an oversized response cannot exhaust memory.
"""

import asyncio
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Protocol, Self
from urllib.parse import urlsplit

import httpx

from project_pilot.enrichment.robots import RobotsGate
from project_pilot.errors import EnrichmentError, SourceBlockedError

type Sleeper = Callable[[float], Awaitable[None]]
# Resolve a host to the IP addresses it points at. Injectable so tests never hit
# real DNS; production uses ``system_resolver``.
type HostResolver = Callable[[str], Awaitable[Sequence[str]]]

# Company pages are HTML; anything bigger is not contact data worth parsing.
_MAX_RESPONSE_BYTES = 2_000_000
# A company site needs at most a hop or two (www ↔ apex, http → https); more than
# this is either a loop or an attempt to walk us somewhere.
_MAX_REDIRECTS = 5


async def system_resolver(host: str) -> list[str]:
    """Resolve ``host`` to every A/AAAA address via the system resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def _reject_non_global(address: str, host: str) -> None:
    try:
        parsed = ip_address(address)
    except ValueError as err:
        raise EnrichmentError(f"unparseable address {address!r} for host {host!r}") from err
    if not parsed.is_global:
        raise EnrichmentError(f"refusing non-public address {address} for host {host!r}")


async def validate_target(url: str, resolver: HostResolver = system_resolver) -> None:
    """Refuse targets enrichment must never fetch.

    Rejects non-http(s) schemes and any host that is — or DNS-resolves to — a
    non-public address (loopback, private, link-local, cloud metadata). Every
    resolved address is checked, not just the first, so a domain that answers with
    a mix of a public and a private record is still refused — this is what closes
    the "attacker registers a domain pointing at 192.168.x/169.254.169.254" hole.

    A narrow DNS-rebinding TOCTOU remains between this validation and httpx's own
    resolution at connect time; for this opt-in, single-user tool that residual is
    accepted, but validating all records stops a straightforwardly hostile domain.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EnrichmentError(f"unsupported URL scheme in {url!r}")
    host = parts.hostname or ""
    if not host:
        raise EnrichmentError(f"missing host in {url!r}")
    try:
        literal = ip_address(host)
    except ValueError:
        addresses = await resolver(host)
        if not addresses:
            raise EnrichmentError(f"host {host!r} did not resolve to any address") from None
        for address in addresses:
            _reject_non_global(address, host)
        return
    if not literal.is_global:
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
    """Fetches pages across many hosts, politely, robots-aware, and SSRF-guarded."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 15.0,
        delay: float = 1.5,
        respect_robots: bool = True,
        sleeper: Sleeper = asyncio.sleep,
        resolver: HostResolver = system_resolver,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._delay = delay
        self._respect_robots = respect_robots
        self._sleeper = sleeper
        self._resolver = resolver
        self._owns_client = client is None
        # Redirects are followed manually (see ``_get_validated``) so each hop is
        # re-validated before it is fetched; auto-following would fetch first.
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=False
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

        The SSRF target check runs first, then the robots check (so the sleep can
        honor the host's Crawl-delay, never below the configured spacing delay).
        """
        await validate_target(url, self._resolver)
        if self._respect_robots and not await self._robots.allowed(url):
            raise SourceBlockedError(f"robots.txt disallows {url}")
        if self._delay_pending:
            await self._sleeper(self._effective_delay(url))
        self._delay_pending = True
        return await self._get_validated(url)

    async def _get_validated(self, url: str) -> FetchedPage:
        """GET ``url``, following redirects manually and re-validating each hop."""
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            async with self._client.stream("GET", current) as response:
                if response.status_code == 403:
                    raise SourceBlockedError(f"HTTP 403 for {current}")
                if response.is_redirect:
                    location = response.headers.get("location")
                    if location is None:
                        raise EnrichmentError(f"redirect without a location from {current}")
                    current = str(response.url.join(location))
                    # A redirect must not pivot the fetch onto a private target, so
                    # re-validate (scheme + resolve) before following it.
                    await validate_target(current, self._resolver)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if (
                    content_type
                    and "html" not in content_type
                    and not content_type.startswith("text/")
                ):
                    raise EnrichmentError(f"not a text page ({content_type}): {current}")
                return FetchedPage(
                    url=str(response.url), text=await _read_capped(response, current)
                )
        raise EnrichmentError(f"too many redirects starting at {url}")

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


async def _read_capped(response: httpx.Response, url: str) -> str:
    """Stream the body, aborting once it exceeds the byte budget, then decode it.

    Streaming caps peak memory: an oversized (or hostile) response is rejected as
    soon as it crosses the limit instead of after the whole body is buffered.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise EnrichmentError(f"response too large (> {_MAX_RESPONSE_BYTES} bytes): {url}")
        chunks.append(chunk)
    encoding = response.charset_encoding or "utf-8"
    try:
        return b"".join(chunks).decode(encoding, errors="replace")
    except LookupError:  # an unknown charset label in the header
        return b"".join(chunks).decode("utf-8", errors="replace")

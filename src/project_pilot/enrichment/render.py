"""Optional headless-browser fetcher for JS-rendered company pages.

Many company sites inject their Impressum/contact data via JavaScript, which the
plain httpx fetcher never sees. ``PlaywrightFetcher`` renders the page in a headless
Chromium and returns the resulting HTML, keeping the same compliance posture as the
httpx fetcher: identifying user agent, timeout, polite spacing delay, the shared
robots gate, and a hard stop (never a retry) on 403.

``playwright`` is an **optional** dependency (``pip install 'project-pilot[render]'``
plus ``playwright install chromium``); it is imported lazily so the module — and the
default install — work without it. Selected via ``ENRICHMENT_RENDER=true``.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx

from project_pilot.enrichment.fetch import FetchedPage, validate_target
from project_pilot.enrichment.robots import RobotsGate
from project_pilot.errors import SourceBlockedError

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright

type Sleeper = Callable[[float], Awaitable[None]]


class PlaywrightFetcher:
    """Renders pages with a headless Chromium; satisfies the ``Fetcher`` protocol."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 20.0,
        delay: float = 1.5,
        respect_robots: bool = True,
        executable_path: str | None = None,
        sleeper: Sleeper = asyncio.sleep,
        robots_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout_ms = int(timeout * 1000)
        self._delay = delay
        self._respect_robots = respect_robots
        self._executable_path = executable_path or None
        self._sleeper = sleeper
        self._owns_robots_client = robots_client is None
        self._robots_client = robots_client or httpx.AsyncClient(
            headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True
        )
        self._robots = RobotsGate(user_agent, self._fetch_robots)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._delay_pending = False

    async def fetch(self, url: str) -> FetchedPage:
        """Render ``url`` after the polite delay; raise ``SourceBlockedError`` if blocked.

        Same guard order as ``WebFetcher.fetch``: target validation and the robots
        check run first so the sleep can honor the host's Crawl-delay.
        """
        validate_target(url)
        if self._respect_robots and not await self._robots.allowed(url):
            raise SourceBlockedError(f"robots.txt disallows {url}")
        if self._delay_pending:
            await self._sleeper(self._effective_delay(url))
        self._delay_pending = True
        return await self._render(url)

    def _effective_delay(self, url: str) -> float:
        if not self._respect_robots:
            return self._delay
        return max(self._delay, self._robots.crawl_delay(url) or 0.0)

    async def _render(self, url: str) -> FetchedPage:  # pragma: no cover - browser boundary
        browser = await self._ensure_browser()
        context = await browser.new_context(user_agent=self._user_agent)
        try:
            page = await context.new_page()
            response = await page.goto(url, timeout=self._timeout_ms, wait_until="domcontentloaded")
            if response is not None and response.status == 403:
                raise SourceBlockedError(f"HTTP 403 for {url}")
            return FetchedPage(url=page.url, text=await page.content())
        finally:
            await context.close()

    async def _ensure_browser(self) -> "Browser":  # pragma: no cover - browser boundary
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True, executable_path=self._executable_path
            )
        return self._browser

    async def _fetch_robots(self, robots_url: str) -> str | None:
        try:
            response = await self._robots_client.get(robots_url)
        except httpx.HTTPError:
            return None  # unreachable robots.txt: fail open (best effort)
        return response.text if response.status_code < 400 else None

    async def aclose(self) -> None:
        if self._browser is not None:  # pragma: no cover - browser boundary
            await self._browser.close()
        if self._playwright is not None:  # pragma: no cover - browser boundary
            await self._playwright.stop()
        if self._owns_robots_client:
            await self._robots_client.aclose()

"""Politeness httpx client: user agent, robots.txt gate, delays, timeouts, retries."""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Self
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)
from tenacity.wait import wait_base

from project_pilot.errors import ConfigError, SourceBlockedError

BASE_URL = "https://www.freelancermap.de"

_CAPTCHA_MARKERS = (
    "g-recaptcha",
    "hcaptcha",
    "captcha-container",
    "please verify you are human",
    "unusual traffic",
)

type Sleeper = Callable[[float], Awaitable[None]]


class _RetryableResponseError(Exception):
    """Wraps a 429/5xx response so tenacity retries it (never used for 403)."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"retryable status {response.status_code}")
        self.response = response


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


class PolitenessClient:
    """A compliance-first HTTP client: identifying UA, robots gate, spaced requests.

    Transient failures (network errors, 5xx, 429) are retried with backoff; a 403
    or captcha is never retried and raises ``SourceBlockedError`` immediately.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        base_url: str = BASE_URL,
        min_delay: float = 2.0,
        max_delay: float = 5.0,
        timeout: float = 20.0,
        sleeper: Sleeper = asyncio.sleep,
        max_attempts: int = 3,
        retry_wait: wait_base | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._base_url = base_url.rstrip("/")
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._effective_delay = min_delay
        self._sleeper = sleeper
        self._max_attempts = max_attempts
        self._retry_wait: wait_base = retry_wait or wait_exponential_jitter(initial=1.0, max=10.0)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
        self._delay_pending = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def effective_delay(self) -> float:
        return self._effective_delay

    async def check_robots(self, urls: list[str]) -> None:
        """Fetch robots.txt and abort (ConfigError) if any URL is disallowed."""
        parser = RobotFileParser()
        response = await self._client.get(f"{self._base_url}/robots.txt")
        if response.status_code >= 400:
            parser.parse([])
        else:
            parser.parse(response.text.splitlines())
        for url in urls:
            if not parser.can_fetch(self._user_agent, url):
                raise ConfigError(f"robots.txt disallows {url!r} for this user agent; aborting")
        crawl_delay = parser.crawl_delay(self._user_agent)
        if crawl_delay is not None:
            self._effective_delay = max(self._min_delay, float(crawl_delay))

    async def get(self, url: str) -> httpx.Response:
        """Fetch after the polite delay; raise SourceBlockedError on 403 or a captcha."""
        if self._delay_pending:
            await self._sleeper(self._next_delay())
        self._delay_pending = True
        try:
            response = await self._fetch(url)
        except _RetryableResponseError as exhausted:
            response = exhausted.response
        if response.status_code == 403:
            raise SourceBlockedError(f"HTTP 403 for {url}")
        if _looks_like_captcha(response):
            raise SourceBlockedError(f"captcha/bot wall for {url}")
        return response

    async def _fetch(self, url: str) -> httpx.Response:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.TransportError, _RetryableResponseError)),
            wait=self._retry_wait,
            stop=stop_after_attempt(self._max_attempts),
            reraise=True,
        ):
            with attempt:
                response = await self._client.get(url)
                if _is_retryable_status(response.status_code):
                    raise _RetryableResponseError(response)
                return response
        raise AssertionError("unreachable")  # pragma: no cover

    def _next_delay(self) -> float:
        lower = self._effective_delay
        upper = max(self._effective_delay, self._max_delay)
        return random.uniform(lower, upper)


def _looks_like_captcha(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    if content_type and "html" not in content_type:
        return False
    body = response.text.lower()
    return any(marker in body for marker in _CAPTCHA_MARKERS)

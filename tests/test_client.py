"""Tests for the PolitenessClient (respx-mocked; no live requests)."""

import httpx
import pytest
import respx

from project_pilot.errors import ConfigError, SourceBlockedError
from project_pilot.ingestion.client import PolitenessClient

BASE = "https://www.freelancermap.de"
UA = "project-pilot/1.0 (personal project alert bot; contact: nik@example.com)"


async def _noop(_seconds: float) -> None:
    return None


@respx.mock
async def test_get_sends_user_agent_and_returns_body() -> None:
    route = respx.get(f"{BASE}/projekt/x-1").mock(
        return_value=httpx.Response(200, html="<h1>ok</h1>")
    )
    async with PolitenessClient(user_agent=UA, sleeper=_noop) as client:
        response = await client.get(f"{BASE}/projekt/x-1")
    assert response.status_code == 200
    assert route.calls.last.request.headers["user-agent"] == UA


@respx.mock
async def test_check_robots_allows() -> None:
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    async with PolitenessClient(user_agent=UA, sleeper=_noop) as client:
        await client.check_robots([f"{BASE}/projektboerse"])


@respx.mock
async def test_check_robots_disallow_raises() -> None:
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /projektboerse\n")
    )
    async with PolitenessClient(user_agent=UA, sleeper=_noop) as client:
        with pytest.raises(ConfigError):
            await client.check_robots([f"{BASE}/projektboerse/suche"])


@respx.mock
async def test_check_robots_honors_crawl_delay() -> None:
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 10\nAllow: /\n")
    )
    async with PolitenessClient(user_agent=UA, min_delay=2.0, sleeper=_noop) as client:
        await client.check_robots([f"{BASE}/x"])
        assert client.effective_delay == 10.0


@respx.mock
async def test_get_403_raises_source_blocked() -> None:
    respx.get(f"{BASE}/blocked").mock(return_value=httpx.Response(403, text="forbidden"))
    async with PolitenessClient(user_agent=UA, sleeper=_noop) as client:
        with pytest.raises(SourceBlockedError):
            await client.get(f"{BASE}/blocked")


@respx.mock
async def test_get_captcha_raises_source_blocked() -> None:
    respx.get(f"{BASE}/captcha").mock(
        return_value=httpx.Response(
            200, html="<div class='g-recaptcha'>please verify you are human</div>"
        )
    )
    async with PolitenessClient(user_agent=UA, sleeper=_noop) as client:
        with pytest.raises(SourceBlockedError):
            await client.get(f"{BASE}/captcha")


@respx.mock
async def test_delay_applied_between_requests_only() -> None:
    respx.get(f"{BASE}/a").mock(return_value=httpx.Response(200, html="a"))
    respx.get(f"{BASE}/b").mock(return_value=httpx.Response(200, html="b"))
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    async with PolitenessClient(
        user_agent=UA, min_delay=2.0, max_delay=5.0, sleeper=record
    ) as client:
        await client.get(f"{BASE}/a")
        await client.get(f"{BASE}/b")

    assert len(delays) == 1  # no delay before the first request
    assert 2.0 <= delays[0] <= 5.0

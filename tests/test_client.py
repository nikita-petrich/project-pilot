"""Tests for the PolitenessClient (respx-mocked; no live requests)."""

import httpx
import pytest
import respx
from tenacity import wait_none

from project_pilot.errors import ConfigError, SourceBlockedError, SourceUnavailableError
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
async def test_check_robots_retries_on_transport_error() -> None:
    route = respx.get(f"{BASE}/robots.txt").mock(
        side_effect=[
            httpx.ConnectError("All connection attempts failed"),
            httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        ]
    )
    async with PolitenessClient(user_agent=UA, sleeper=_noop, retry_wait=wait_none()) as client:
        await client.check_robots([f"{BASE}/projektboerse"])
    assert route.call_count == 2


@respx.mock
async def test_check_robots_unreachable_raises_source_unavailable() -> None:
    route = respx.get(f"{BASE}/robots.txt").mock(
        side_effect=httpx.ConnectError("All connection attempts failed")
    )
    async with PolitenessClient(
        user_agent=UA, sleeper=_noop, retry_wait=wait_none(), max_attempts=2
    ) as client:
        with pytest.raises(SourceUnavailableError, match="unreachable after 2 attempts"):
            await client.check_robots([f"{BASE}/projektboerse"])
    assert route.call_count == 2


@respx.mock
async def test_get_unreachable_raises_source_unavailable() -> None:
    respx.get(f"{BASE}/net").mock(side_effect=httpx.ConnectTimeout("timed out"))
    async with PolitenessClient(
        user_agent=UA, sleeper=_noop, retry_wait=wait_none(), max_attempts=2
    ) as client:
        with pytest.raises(SourceUnavailableError, match="ConnectTimeout"):
            await client.get(f"{BASE}/net")


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


@respx.mock
async def test_get_retries_on_5xx_then_succeeds() -> None:
    route = respx.get(f"{BASE}/flaky").mock(
        side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200, html="ok")]
    )
    async with PolitenessClient(user_agent=UA, sleeper=_noop, retry_wait=wait_none()) as client:
        response = await client.get(f"{BASE}/flaky")
    assert response.status_code == 200
    assert route.call_count == 3


@respx.mock
async def test_get_retries_on_transport_error() -> None:
    route = respx.get(f"{BASE}/net").mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(200, html="ok")]
    )
    async with PolitenessClient(user_agent=UA, sleeper=_noop, retry_wait=wait_none()) as client:
        response = await client.get(f"{BASE}/net")
    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_get_403_is_not_retried() -> None:
    route = respx.get(f"{BASE}/blocked").mock(return_value=httpx.Response(403))
    async with PolitenessClient(user_agent=UA, sleeper=_noop, retry_wait=wait_none()) as client:
        with pytest.raises(SourceBlockedError):
            await client.get(f"{BASE}/blocked")
    assert route.call_count == 1


@respx.mock
async def test_get_retries_exhausted_raises_source_unavailable() -> None:
    route = respx.get(f"{BASE}/down").mock(return_value=httpx.Response(503))
    async with PolitenessClient(
        user_agent=UA, sleeper=_noop, retry_wait=wait_none(), max_attempts=2
    ) as client:
        with pytest.raises(SourceUnavailableError, match="after 2 attempts"):
            await client.get(f"{BASE}/down")
    assert route.call_count == 2


@respx.mock
async def test_check_robots_403_raises_source_blocked() -> None:
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(403))
    async with PolitenessClient(user_agent=UA, sleeper=_noop) as client:
        with pytest.raises(SourceBlockedError, match=r"robots\.txt returned HTTP 403"):
            await client.check_robots([f"{BASE}/projektboerse"])


@respx.mock
async def test_check_robots_404_means_no_rules() -> None:
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(404))
    async with PolitenessClient(user_agent=UA, sleeper=_noop) as client:
        await client.check_robots([f"{BASE}/projektboerse"])


@respx.mock
async def test_check_robots_5xx_raises_source_unavailable() -> None:
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(503))
    async with PolitenessClient(
        user_agent=UA, sleeper=_noop, retry_wait=wait_none(), max_attempts=2
    ) as client:
        with pytest.raises(SourceUnavailableError, match="after 2 attempts"):
            await client.check_robots([f"{BASE}/projektboerse"])


@respx.mock
async def test_get_enforces_robots_for_later_urls() -> None:
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /projekt/\n")
    )
    async with PolitenessClient(user_agent=UA, sleeper=_noop) as client:
        await client.check_robots([f"{BASE}/projektboerse"])  # the checked URL is allowed
        with pytest.raises(SourceBlockedError, match=r"robots\.txt disallows"):
            await client.get(f"{BASE}/projekt/x-1")  # a later disallowed path is refused


@respx.mock
async def test_captcha_mention_in_real_listing_page_is_not_blocked() -> None:
    body = (
        "<script class='js-react-on-rails-component'>{}</script>"
        "<p>Wir suchen Unterstützung bei der hCaptcha-Integration.</p>"
    )
    respx.get(f"{BASE}/projekt/captcha-job").mock(return_value=httpx.Response(200, html=body))
    async with PolitenessClient(user_agent=UA, sleeper=_noop) as client:
        response = await client.get(f"{BASE}/projekt/captcha-job")
    assert response.status_code == 200

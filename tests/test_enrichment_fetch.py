"""Tests for the polite WebFetcher (HTTP mocked with respx, never live)."""

import httpx
import pytest
import respx

from project_pilot.enrichment.fetch import WebFetcher
from project_pilot.errors import EnrichmentError, SourceBlockedError


async def _noop_sleep(_seconds: float) -> None:
    return None


def _fetcher(**kwargs: object) -> WebFetcher:
    return WebFetcher(user_agent="test-agent/1.0", sleeper=_noop_sleep, **kwargs)  # type: ignore[arg-type]


@respx.mock
async def test_fetch_returns_page_text_when_allowed() -> None:
    respx.get("https://firma.de/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get("https://firma.de/impressum").mock(
        return_value=httpx.Response(200, text="<html>Impressum</html>")
    )
    fetcher = _fetcher()
    try:
        page = await fetcher.fetch("https://firma.de/impressum")
    finally:
        await fetcher.aclose()
    assert page.text == "<html>Impressum</html>"


@respx.mock
async def test_fetch_respects_robots_disallow() -> None:
    respx.get("https://firma.de/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
    )
    fetcher = _fetcher()
    try:
        with pytest.raises(SourceBlockedError):
            await fetcher.fetch("https://firma.de/private/data")
    finally:
        await fetcher.aclose()


@respx.mock
async def test_fetch_raises_on_403() -> None:
    respx.get("https://firma.de/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://firma.de/").mock(return_value=httpx.Response(403))
    fetcher = _fetcher()
    try:
        with pytest.raises(SourceBlockedError):
            await fetcher.fetch("https://firma.de/")
    finally:
        await fetcher.aclose()


@respx.mock
async def test_fetch_can_ignore_robots() -> None:
    respx.get("https://firma.de/x").mock(return_value=httpx.Response(200, text="ok"))
    fetcher = _fetcher(respect_robots=False)
    try:
        page = await fetcher.fetch("https://firma.de/x")
    finally:
        await fetcher.aclose()
    assert page.text == "ok"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://firma.de/kontakt",
        "http://192.168.1.10/impressum",
        "http://127.0.0.1:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
    ],
)
async def test_fetch_refuses_unsafe_targets(url: str) -> None:
    fetcher = _fetcher()
    try:
        with pytest.raises(EnrichmentError):
            await fetcher.fetch(url)
    finally:
        await fetcher.aclose()


@respx.mock
async def test_fetch_refuses_redirect_to_private_target() -> None:
    respx.get("https://firma.de/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://firma.de/kontakt").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/steal"})
    )
    respx.get("http://127.0.0.1/steal").mock(return_value=httpx.Response(200, text="secret"))
    fetcher = _fetcher()
    try:
        with pytest.raises(EnrichmentError, match="non-public"):
            await fetcher.fetch("https://firma.de/kontakt")
    finally:
        await fetcher.aclose()


@respx.mock
async def test_fetch_rejects_non_text_content() -> None:
    respx.get("https://firma.de/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://firma.de/broschuere.pdf").mock(
        return_value=httpx.Response(
            200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}
        )
    )
    fetcher = _fetcher()
    try:
        with pytest.raises(EnrichmentError, match="not a text page"):
            await fetcher.fetch("https://firma.de/broschuere.pdf")
    finally:
        await fetcher.aclose()


@respx.mock
async def test_fetch_rejects_oversized_response() -> None:
    respx.get("https://firma.de/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://firma.de/riesig").mock(
        return_value=httpx.Response(200, text="x" * 2_000_001)
    )
    fetcher = _fetcher()
    try:
        with pytest.raises(EnrichmentError, match="too large"):
            await fetcher.fetch("https://firma.de/riesig")
    finally:
        await fetcher.aclose()


@respx.mock
async def test_fetch_honors_crawl_delay_over_base_delay() -> None:
    respx.get("https://firma.de/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 9\nAllow: /")
    )
    respx.get("https://firma.de/a").mock(return_value=httpx.Response(200, text="a"))
    respx.get("https://firma.de/b").mock(return_value=httpx.Response(200, text="b"))
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    fetcher = WebFetcher(user_agent="test-agent/1.0", delay=1.5, sleeper=record)
    try:
        await fetcher.fetch("https://firma.de/a")
        await fetcher.fetch("https://firma.de/b")
    finally:
        await fetcher.aclose()
    assert delays == [9.0]  # no delay before the first fetch; then the host's Crawl-delay

"""Tests for the polite WebFetcher (HTTP mocked with respx, never live)."""

import httpx
import pytest
import respx

from project_pilot.enrichment.fetch import WebFetcher
from project_pilot.errors import SourceBlockedError


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

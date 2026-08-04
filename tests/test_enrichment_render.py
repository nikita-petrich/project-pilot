"""Tests for PlaywrightFetcher's robots gate and cleanup (no browser launched).

The rendering itself is a browser boundary (covered out); these tests exercise the
compliance guard that runs *before* any browser is started.
"""

import httpx
import pytest
import respx

from project_pilot.enrichment.render import PlaywrightFetcher
from project_pilot.errors import EnrichmentError, SourceBlockedError


async def _noop_sleep(_seconds: float) -> None:
    return None


@respx.mock
async def test_render_fetcher_blocks_disallowed_paths_before_launching() -> None:
    respx.get("https://firma.de/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
    )
    fetcher = PlaywrightFetcher(user_agent="test-agent/1.0", sleeper=_noop_sleep)
    try:
        with pytest.raises(SourceBlockedError):
            await fetcher.fetch("https://firma.de/private/data")
    finally:
        await fetcher.aclose()  # closes the robots client without a browser present


async def test_render_fetcher_refuses_private_targets_before_launching() -> None:
    fetcher = PlaywrightFetcher(user_agent="test-agent/1.0", sleeper=_noop_sleep)
    try:
        with pytest.raises(EnrichmentError):
            await fetcher.fetch("http://127.0.0.1/impressum")
    finally:
        await fetcher.aclose()

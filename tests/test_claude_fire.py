"""Routine fire client: text building, retries, and failure behavior."""

import httpx
import respx

from project_pilot.notification.claude_fire import (
    MAX_TEXT_CHARS,
    ClaudeRoutineFire,
    fire_text,
)
from project_pilot.notification.messages import MatchMessage

FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/trig_test/fire"


def _message(
    description: str = "Volltext der Ausschreibung.", listing_id: int | None = 42
) -> MatchMessage:
    return MatchMessage(
        title="Senior Python Developer",
        url="https://example.com/p/1",
        score=87,
        listing_id=listing_id,
        company="ACME GmbH",
        location="Remote (DE)",
        reasons=["Stack passt", "Remote"],
        risk_flags=["kein Budget genannt"],
        skills=["Python", "FastAPI"],
        description=description,
    )


def _client() -> ClaudeRoutineFire:
    return ClaudeRoutineFire(fire_url=FIRE_URL, token="sk-ant-oat01-test")


def test_fire_text_carries_facts_and_description() -> None:
    text = fire_text(_message())
    assert "Score 87/100" in text
    assert "Firma: ACME GmbH" in text
    assert "Warum Match: Stack passt · Remote" in text
    assert text.endswith("Volltext der Ausschreibung.")


def test_fire_text_carries_the_listing_id_for_the_mcp_tools() -> None:
    # The thread needs the id to call get_listing/draft_application; a listing that
    # is not stored (a manual check) has none, and must not fake one.
    assert "Listing-ID: 42" in fire_text(_message())
    assert "Listing-ID" not in fire_text(_message(listing_id=None))


def test_fire_text_is_capped() -> None:
    text = fire_text(_message(description="x" * (2 * MAX_TEXT_CHARS)))
    assert len(text) == MAX_TEXT_CHARS


@respx.mock
async def test_fire_returns_session_url_and_sends_headers() -> None:
    route = respx.post(FIRE_URL).respond(
        200, json={"claude_code_session_url": "https://claude.ai/code/session_01X"}
    )
    url = await _client().fire(_message())
    assert url == "https://claude.ai/code/session_01X"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-ant-oat01-test"
    assert request.headers["anthropic-beta"] == "experimental-cc-routine-2026-04-01"


@respx.mock
async def test_fire_retries_5xx_then_succeeds() -> None:
    route = respx.post(FIRE_URL)
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json={"claude_code_session_url": "https://claude.ai/code/s2"}),
    ]
    assert await _client().fire(_message()) == "https://claude.ai/code/s2"
    assert route.call_count == 2


@respx.mock
async def test_fire_does_not_retry_4xx_and_returns_none() -> None:
    route = respx.post(FIRE_URL).respond(401, json={"type": "error"})
    assert await _client().fire(_message()) is None
    assert route.call_count == 1  # a bad token never burns retries


@respx.mock
async def test_fire_swallows_network_errors() -> None:
    respx.post(FIRE_URL).side_effect = httpx.ConnectError("down")
    assert await _client().fire(_message()) is None

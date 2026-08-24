"""ntfy push client: payload shape, deep link, retries, and failure behavior."""

import json

import httpx
import pytest
import respx

from project_pilot.errors import ConfigError
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.push import (
    MAX_BODY_CHARS,
    PRIORITY_MATCH,
    PRIORITY_WARNING,
    NtfyPush,
    push_body,
    split_topic_url,
)

TOPIC_URL = "https://ntfy.sh/pilot-test"
SERVER = "https://ntfy.sh"
PROJECT_URL = "https://claude.ai/cowork/project/01a032c6-7b1d-728a-a279-78c39ce45076"


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


def _client() -> NtfyPush:
    return NtfyPush(topic_url=TOPIC_URL, token="tk_secret", target_url=PROJECT_URL)


def test_split_topic_url_separates_server_and_topic() -> None:
    assert split_topic_url("https://ntfy.sh/pilot-test/") == (SERVER, "pilot-test")
    assert split_topic_url("https://ntfy.sequenz.io/matches") == (
        "https://ntfy.sequenz.io",
        "matches",
    )


@pytest.mark.parametrize(
    "bad", ["", "ntfy.sh/topic", "https://ntfy.sh", "https://ntfy.sh/a/b", "not a url"]
)
def test_split_topic_url_rejects_anything_that_is_not_server_plus_topic(bad: str) -> None:
    # A silently wrong topic would push into the void, so this fails at wiring time.
    with pytest.raises(ConfigError):
        split_topic_url(bad)


def test_push_body_leads_with_the_command_to_type() -> None:
    body = push_body(_message())
    lines = body.splitlines()
    # The chat the push opens is empty, so the body carries the exact command.
    assert lines[0] == "→ /check-project 42"
    assert lines[2] == "🎯 Senior Python Developer  ·  87/100"
    assert "✅ Fits: Stack passt, Remote" in body
    assert "⚠️ Risks: kein Budget genannt" in body


def test_push_body_omits_the_command_for_an_unstored_listing() -> None:
    # A manual check has no id; a "/check-project None" would be a dead command.
    assert "check-project" not in push_body(_message(listing_id=None))


def test_push_body_is_capped() -> None:
    # ntfy drops anything past its own limit, so the card is trimmed here instead.
    message = MatchMessage(
        title="T" * (2 * MAX_BODY_CHARS),
        url="https://example.com/p/1",
        score=87,
    )
    assert len(push_body(message)) == MAX_BODY_CHARS


@respx.mock
async def test_notify_posts_title_card_and_click_target() -> None:
    route = respx.post(SERVER).respond(200, json={"id": "abc"})
    assert await _client().notify(_message()) is True
    payload = json.loads(route.calls.last.request.read())
    assert payload["topic"] == "pilot-test"
    # The headline is what shows on the lock screen, so it leads with score and company.
    assert payload["title"] == "⭐ 87 · Senior Python Developer · ACME GmbH"
    assert payload["priority"] == PRIORITY_MATCH
    assert payload["click"] == PROJECT_URL  # one tap lands in the match project
    assert payload["message"] == push_body(_message())
    assert route.calls.last.request.headers["authorization"] == "Bearer tk_secret"


@respx.mock
async def test_click_falls_back_to_the_listing_when_no_project_is_configured() -> None:
    route = respx.post(SERVER).respond(200, json={"id": "abc"})
    await NtfyPush(topic_url=TOPIC_URL).notify(_message())
    # Never a dead push: without a project the tap opens the listing itself.
    assert json.loads(route.calls.last.request.read())["click"] == "https://example.com/p/1"


@respx.mock
async def test_notify_sends_emoji_as_utf8_not_escaped_headers() -> None:
    # The reason this publishes JSON: titles carry emoji and umlauts, which HTTP
    # headers (ntfy's X-Title) cannot carry safely.
    route = respx.post(SERVER).respond(200, json={"id": "abc"})
    await _client().notify(_message())
    assert "⭐".encode() in route.calls.last.request.read()


@respx.mock
async def test_notify_warning_uses_max_priority() -> None:
    route = respx.post(SERVER).respond(200, json={"id": "abc"})
    assert await _client().notify_warning("Quelle im Cooldown") is True
    payload = json.loads(route.calls.last.request.read())
    assert payload["priority"] == PRIORITY_WARNING
    assert payload["message"] == "Quelle im Cooldown"


@respx.mock
async def test_notify_retries_5xx_then_succeeds() -> None:
    route = respx.post(SERVER)
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={"id": "a"})]
    assert await _client().notify(_message()) is True
    assert route.call_count == 2


@respx.mock
async def test_notify_does_not_retry_4xx_and_returns_none() -> None:
    route = respx.post(SERVER).respond(403, json={"error": "forbidden"})
    assert await _client().notify(_message()) is False
    assert route.call_count == 1  # a bad token never burns retries


@respx.mock
async def test_notify_swallows_network_errors() -> None:
    respx.post(SERVER).side_effect = httpx.ConnectError("down")
    assert await _client().notify(_message()) is False
    assert await _client().notify_warning("x") is False


def test_no_token_sends_no_authorization_header() -> None:
    # ntfy.sh public topics take no auth; an empty header would be rejected.
    assert NtfyPush(topic_url=TOPIC_URL)._headers == {}

"""Tests for the Telegram client and message formatting (respx-mocked)."""

import json

import httpx
import respx

from project_pilot.notification.telegram import (
    MatchMessage,
    TelegramClient,
    build_digest,
    format_match,
)

API = "https://api.telegram.org/bottok:123/sendMessage"


def _message(title: str = "Py Dev", score: int = 80) -> MatchMessage:
    return MatchMessage(
        title=title,
        url="https://x/1",
        score=score,
        reasons=["fits the profile", "remote"],
        start="ab sofort",
        location="Remote",
        remote="remote",
    )


def test_format_match_has_link_score_reasons() -> None:
    text = format_match(_message())
    assert '<a href="https://x/1">' in text
    assert "Py Dev" in text
    assert "Score: 80" in text
    assert "fits the profile" in text


def test_format_match_escapes_html() -> None:
    text = format_match(_message(title="A <b>& B"))
    assert "&lt;b&gt;" in text
    assert "&amp;" in text


def test_format_match_caps_reasons_at_three() -> None:
    message = MatchMessage(
        title="T",
        url="https://x",
        score=50,
        reasons=["r1", "r2", "r3", "r4"],
        start=None,
        location=None,
        remote="remote",
    )
    assert "r4" not in format_match(message)


def test_build_digest_multiple() -> None:
    text = build_digest([_message(title="Alpha"), _message(title="Beta")])
    assert "2 new matches" in text
    assert "Alpha" in text
    assert "Beta" in text


def test_build_digest_singular_and_empty() -> None:
    assert "1 new match" in build_digest([_message()])
    assert "matches" not in build_digest([_message()])
    assert build_digest([]) == ""


@respx.mock
async def test_send_message_success() -> None:
    route = respx.post(API).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    async with TelegramClient(bot_token="tok:123", chat_id="42") as client:
        assert await client.send_message("hi") is True
    body = json.loads(route.calls.last.request.content)
    assert body["chat_id"] == "42"
    assert body["parse_mode"] == "HTML"


@respx.mock
async def test_send_message_api_error_returns_false() -> None:
    respx.post(API).mock(return_value=httpx.Response(200, json={"ok": False, "description": "bad"}))
    async with TelegramClient(bot_token="tok:123", chat_id="42") as client:
        assert await client.send_message("hi") is False


@respx.mock
async def test_send_message_transport_error_returns_false() -> None:
    respx.post(API).mock(side_effect=httpx.ConnectError("down"))
    async with TelegramClient(bot_token="tok:123", chat_id="42") as client:
        assert await client.send_message("hi") is False


@respx.mock
async def test_send_message_bad_status_returns_false() -> None:
    respx.post(API).mock(return_value=httpx.Response(500, text="server error"))
    async with TelegramClient(bot_token="tok:123", chat_id="42") as client:
        assert await client.send_message("hi") is False

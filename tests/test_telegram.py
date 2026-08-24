"""Telegram notifier: message shape, the inline button, retries, and failures."""

import json

import httpx
import pytest
import respx

from project_pilot.config import Settings
from project_pilot.errors import ConfigError
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.telegram import (
    MAX_TEXT_CHARS,
    TelegramNotifier,
    match_text,
)

BOT_TOKEN = "123456:AAtest-token"
CHAT_ID = "987654321"
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
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


def _notifier(*, target_url: str = PROJECT_URL) -> TelegramNotifier:
    return TelegramNotifier(bot_token=BOT_TOKEN, chat_id=CHAT_ID, target_url=target_url)


def test_match_text_leads_with_headline_then_the_command_then_the_card() -> None:
    lines = match_text(_message()).splitlines()
    # The headline is what a notification preview shows.
    assert lines[0] == "⭐ 87 · Senior Python Developer · ACME GmbH"
    # The chat the button opens starts empty, so the body carries the command.
    assert lines[2] == "→ /check-project 42"
    assert lines[4] == "🎯 Senior Python Developer  ·  87/100"
    assert "✅ Fits: Stack passt, Remote" in match_text(_message())
    assert "⚠️ Risks: kein Budget genannt" in match_text(_message())


def test_match_text_omits_the_command_for_an_unstored_listing() -> None:
    # A manual check has no id; a "/check-project None" would be a dead command.
    assert "check-project" not in match_text(_message(listing_id=None))


def test_match_text_is_capped_below_the_telegram_limit() -> None:
    # Telegram rejects a message past 4096 characters outright, losing the match.
    long = MatchMessage(title="T" * (2 * MAX_TEXT_CHARS), url="https://example.com/p/1", score=87)
    assert len(match_text(long)) == MAX_TEXT_CHARS


@respx.mock
async def test_notify_sends_the_card_and_a_button_to_the_project() -> None:
    route = respx.post(SEND_URL).respond(200, json={"ok": True})
    assert await _notifier().notify(_message()) is True
    payload = json.loads(route.calls.last.request.read())
    assert payload["chat_id"] == CHAT_ID
    assert payload["text"] == match_text(_message())
    assert payload["disable_web_page_preview"] is True
    button = payload["reply_markup"]["inline_keyboard"][0][0]
    assert button["url"] == PROJECT_URL
    # No parse_mode: an underscore in a listing title would reject the message.
    assert "parse_mode" not in payload


@respx.mock
async def test_button_falls_back_to_the_listing_when_no_project_is_configured() -> None:
    route = respx.post(SEND_URL).respond(200, json={"ok": True})
    await _notifier(target_url="").notify(_message())
    payload = json.loads(route.calls.last.request.read())
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://example.com/p/1"


@respx.mock
async def test_notify_sends_emoji_as_utf8() -> None:
    route = respx.post(SEND_URL).respond(200, json={"ok": True})
    await _notifier().notify(_message())
    assert "⭐".encode() in route.calls.last.request.read()


@respx.mock
async def test_warning_is_sent_without_a_button() -> None:
    route = respx.post(SEND_URL).respond(200, json={"ok": True})
    assert await _notifier().notify_warning("Quelle im Cooldown") is True
    payload = json.loads(route.calls.last.request.read())
    assert "Quelle im Cooldown" in payload["text"]
    assert "reply_markup" not in payload  # nothing to open for an operator warning


@respx.mock
async def test_notify_retries_5xx_then_succeeds() -> None:
    route = respx.post(SEND_URL)
    route.side_effect = [httpx.Response(502), httpx.Response(200, json={"ok": True})]
    assert await _notifier().notify(_message()) is True
    assert route.call_count == 2


@respx.mock
async def test_notify_does_not_retry_4xx_and_returns_false() -> None:
    route = respx.post(SEND_URL).respond(401, json={"ok": False, "description": "Unauthorized"})
    assert await _notifier().notify(_message()) is False
    assert route.call_count == 1  # a revoked token never burns retries


@respx.mock
async def test_notify_swallows_network_errors() -> None:
    respx.post(SEND_URL).side_effect = httpx.ConnectError("down")
    assert await _notifier().notify(_message()) is False
    assert await _notifier().notify_warning("x") is False


def test_require_telegram_names_the_missing_half(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Settings().require_telegram()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    with pytest.raises(ConfigError, match="TELEGRAM_CHAT_ID"):
        Settings().require_telegram()

    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    assert Settings().require_telegram() == (BOT_TOKEN, CHAT_ID)

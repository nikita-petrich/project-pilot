"""Telegram notifier: message shape, the inline button, retries, and failures."""

import json
from dataclasses import replace

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
SENT = {"ok": True, "result": {"message_id": 5150}}


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


def _notifier() -> TelegramNotifier:
    return TelegramNotifier(bot_token=BOT_TOKEN, chat_id=CHAT_ID)


def test_match_text_leads_with_the_headline_then_every_fact() -> None:
    text = match_text(_message())
    # The headline is what a notification preview shows.
    assert text.splitlines()[0] == "⭐ 87 · Senior Python Developer · ACME GmbH"
    assert "🏢 Company: ACME GmbH" in text
    assert "🎯 Score: 87/100" in text
    assert "✅ Fits: Stack passt, Remote" in text
    assert "🚩 Risks: kein Budget genannt" in text


def test_match_text_names_no_command_to_type_elsewhere() -> None:
    # The work happens in the post's own thread; a pointer at another app's
    # command would send you somewhere you no longer need to go.
    assert "check-project" not in match_text(_message())
    assert "claude.ai" not in match_text(_message())


def test_match_text_is_capped_below_the_telegram_limit() -> None:
    # Telegram rejects a message past 4096 characters outright, losing the match.
    long = MatchMessage(title="T" * (2 * MAX_TEXT_CHARS), url="https://example.com/p/1", score=87)
    assert len(match_text(long)) == MAX_TEXT_CHARS


@respx.mock
async def test_notify_sends_the_card_under_its_three_decisions() -> None:
    route = respx.post(SEND_URL).respond(200, json=SENT)
    assert await _notifier().notify(_message()) == 5150
    payload = json.loads(route.calls.last.request.read())
    assert payload["chat_id"] == CHAT_ID
    assert payload["text"] == match_text(_message())
    assert payload["disable_web_page_preview"] is True
    rows = payload["reply_markup"]["inline_keyboard"]
    assert [button["text"] for row in rows for button in row] == [
        "✅ Annehmen",
        "🚫 Ablehnen",
        "📄 Projektbeschreibung",
    ]
    # Every callback carries the listing, so two open matches cannot be confused.
    assert [button["callback_data"] for row in rows for button in row] == [
        "accept:42",
        "decline:42",
        "describe:42",
    ]
    # No parse_mode: an underscore in a listing title would reject the message.
    assert "parse_mode" not in payload


@respx.mock
async def test_an_unstored_listing_gets_no_buttons() -> None:
    # Without an id there is nothing for a press to act on; a dead button is
    # worse than none.
    route = respx.post(SEND_URL).respond(200, json=SENT)
    await _notifier().notify(replace(_message(), listing_id=None))
    payload = json.loads(route.calls.last.request.read())
    assert "reply_markup" not in payload


@respx.mock
async def test_notify_sends_emoji_as_utf8() -> None:
    route = respx.post(SEND_URL).respond(200, json=SENT)
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
    route.side_effect = [httpx.Response(502), httpx.Response(200, json=SENT)]
    assert await _notifier().notify(_message()) == 5150
    assert route.call_count == 2


@respx.mock
async def test_notify_does_not_retry_4xx_and_returns_false() -> None:
    route = respx.post(SEND_URL).respond(401, json={"ok": False, "description": "Unauthorized"})
    assert await _notifier().notify(_message()) is None
    assert route.call_count == 1  # a revoked token never burns retries


@respx.mock
async def test_notify_swallows_network_errors() -> None:
    respx.post(SEND_URL).side_effect = httpx.ConnectError("down")
    assert await _notifier().notify(_message()) is None
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


@respx.mock
async def test_notify_returns_none_when_telegram_names_no_message_id() -> None:
    """Without the post's id the card can never be tied to its comment thread."""
    respx.post(SEND_URL).respond(200, json={"ok": True, "result": {}})
    assert await _notifier().notify(_message()) is None


@respx.mock
async def test_the_card_goes_to_the_channel_and_names_no_thread() -> None:
    """Telegram roots the thread itself by forwarding the post; nothing to pass."""
    route = respx.post(SEND_URL).respond(200, json=SENT)
    await _notifier().notify(_message())
    payload = json.loads(route.calls.last.request.read())
    assert payload["chat_id"] == CHAT_ID
    assert "message_thread_id" not in payload
    assert "reply_parameters" not in payload


@respx.mock
async def test_warning_carries_no_thread_of_its_own() -> None:
    # An operator warning belongs to the worker, not to any one listing.
    route = respx.post(SEND_URL).respond(200, json={"ok": True})
    await _notifier().notify_warning("Quelle im Cooldown")
    assert "message_thread_id" not in json.loads(route.calls.last.request.read())

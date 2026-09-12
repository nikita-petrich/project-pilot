"""Telegram notifier: message shape, the three buttons, retries, and failures."""

import json
from dataclasses import replace

import httpx
import pytest
import respx

from project_pilot.config import Settings
from project_pilot.errors import ConfigError
from project_pilot.notification.claude_link import session_link
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.telegram import (
    MAX_TEXT_CHARS,
    TelegramNotifier,
    match_keyboard,
    match_text,
)

BOT_TOKEN = "123456:AAtest-token"
CHAT_ID = "987654321"
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
SENT = {"ok": True, "result": {"message_id": 5150}}
CHAT_URL = "https://claude.ai/new"


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
    return TelegramNotifier(bot_token=BOT_TOKEN, chat_id=CHAT_ID, session_url=CHAT_URL)


def test_match_text_leads_with_the_headline_then_every_fact() -> None:
    text = match_text(_message())
    # The headline is what a notification preview shows.
    assert text.splitlines()[0] == "⭐ 87 · Senior Python Developer · ACME GmbH"
    assert "🏢 Company: ACME GmbH" in text
    assert "🎯 Score: 87/100" in text
    assert "✅ Fits: Stack passt, Remote" in text
    assert "🚩 Risks: kein Budget genannt" in text


def test_match_text_names_no_command_to_type_elsewhere() -> None:
    # The session is one tap away on the Bewerben button; the card itself
    # carries no command and no link to type or copy.
    text = match_text(_message())
    assert "check-project" not in text
    assert "claude.ai" not in text


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
    assert rows == [
        [
            # Bewerben opens a new session with this card in its prompt;
            # Ablehnen is the one press the bot hears.
            {"text": "✅ Bewerben", "url": session_link(_message(), base_url=CHAT_URL)},
            {"text": "🚫 Ablehnen", "callback_data": "decline:42"},
        ],
        [{"text": "📄 Projektbeschreibung öffnen", "url": "https://example.com/p/1"}],
    ]
    # No parse_mode: an underscore in a listing title would reject the message.
    assert "parse_mode" not in payload


def test_an_unstored_listing_still_gets_a_working_decline() -> None:
    # test-match stores nothing, so there is no id for Ablehnen to name — but the
    # bot never looks the id up, only deletes the card, so the button still works.
    message = replace(_message(), listing_id=None)
    assert match_keyboard(message, session_url=CHAT_URL) == {
        "inline_keyboard": [
            [
                {"text": "✅ Bewerben", "url": session_link(message, base_url=CHAT_URL)},
                {"text": "🚫 Ablehnen", "callback_data": "decline:"},
            ],
            [{"text": "📄 Projektbeschreibung öffnen", "url": "https://example.com/p/1"}],
        ]
    }


def test_a_listing_without_a_real_link_gets_no_link_button() -> None:
    # An ingested listing may carry a pilot:// placeholder, which Telegram
    # rejects in a URL button — and would reject the whole message with it.
    message = replace(_message(), url="pilot://ingest/abc")
    assert match_keyboard(message) == {
        "inline_keyboard": [
            [
                {"text": "✅ Bewerben", "url": session_link(message)},
                {"text": "🚫 Ablehnen", "callback_data": "decline:42"},
            ]
        ]
    }


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
    """A send whose id is unknown counts as failed, so it is retried next run."""
    respx.post(SEND_URL).respond(200, json={"ok": True, "result": {}})
    assert await _notifier().notify(_message()) is None

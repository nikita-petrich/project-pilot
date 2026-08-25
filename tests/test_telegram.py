"""Telegram notifier: message shape, the inline button, retries, and failures."""

import json

import httpx
import pytest
import respx

from project_pilot.config import Settings
from project_pilot.errors import ConfigError
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.telegram import (
    MAX_DESCRIPTION_CHARS,
    MAX_TEXT_CHARS,
    TelegramNotifier,
    match_keyboard,
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


def _notifier() -> TelegramNotifier:
    return TelegramNotifier(bot_token=BOT_TOKEN, chat_id=CHAT_ID)


def test_match_text_carries_the_whole_listing() -> None:
    # The card decides at a glance; the description is what the decision is
    # actually made on, so it travels in the same message.
    text = match_text(_message(description="Node.js, Express, Docker, Postgres."))
    lines = text.splitlines()
    assert lines[0] == "⭐ 87 · Senior Python Developer · ACME GmbH"
    assert lines[2] == "🎯 Senior Python Developer  ·  87/100"
    assert "✅ Fits: Stack passt, Remote" in text
    assert "Skills: Python, FastAPI" in text
    assert text.endswith("Beschreibung:\nNode.js, Express, Docker, Postgres.")


def test_a_long_description_is_trimmed_not_dropped() -> None:
    text = match_text(_message(description="x" * (MAX_DESCRIPTION_CHARS * 3)))
    assert len(text) <= MAX_TEXT_CHARS
    assert "Beschreibung:" in text
    assert text.endswith("…")


def test_match_text_is_capped_below_the_telegram_limit() -> None:
    # Telegram rejects a message past 4096 characters outright, losing the match.
    long = MatchMessage(title="T" * (2 * MAX_TEXT_CHARS), url="https://example.com/p/1", score=87)
    assert len(match_text(long)) == MAX_TEXT_CHARS


def _rows(message: MatchMessage) -> list[list[dict[str, str]]]:
    """The keyboard's rows, asserting there is one — the caller wants the rows."""
    keyboard = match_keyboard(message)
    assert keyboard is not None
    rows = keyboard["inline_keyboard"]
    assert isinstance(rows, list)
    return rows


def test_the_keyboard_offers_the_link_and_both_decisions() -> None:
    rows = _rows(_message())
    assert rows[0][0] == {"text": "🔗 Projekt öffnen", "url": "https://example.com/p/1"}
    # The listing id rides in the callback so two cards cannot be confused.
    assert rows[1][0]["callback_data"] == "accept:42"
    assert rows[1][1]["callback_data"] == "decline:42"


def test_an_unstored_listing_offers_only_the_link() -> None:
    # A manual check has no id; there is nothing to accept or decline.
    rows = _rows(_message(listing_id=None))
    assert len(rows) == 1
    assert "url" in rows[0][0]


def test_a_listing_without_a_url_still_gets_its_decisions() -> None:
    rows = _rows(MatchMessage(title="T", url="", score=87, listing_id=7))
    assert len(rows) == 1
    assert rows[0][0]["callback_data"] == "accept:7"


@respx.mock
async def test_notify_sends_the_card_with_its_buttons() -> None:
    route = respx.post(SEND_URL).respond(200, json={"ok": True})
    assert await _notifier().notify(_message()) is True

    payload = json.loads(route.calls.last.request.read())
    assert payload["chat_id"] == CHAT_ID
    assert payload["text"] == match_text(_message())
    assert payload["disable_web_page_preview"] is True
    assert payload["reply_markup"] == match_keyboard(_message())
    # No parse_mode: an underscore in a listing title would reject the message.
    assert "parse_mode" not in payload


@respx.mock
async def test_notify_sends_emoji_as_utf8() -> None:
    route = respx.post(SEND_URL).respond(200, json={"ok": True})
    await _notifier().notify(_message())
    assert "⭐".encode() in route.calls.last.request.read()


@respx.mock
async def test_warning_carries_no_buttons() -> None:
    # An operator warning belongs to no listing, so there is nothing to press.
    route = respx.post(SEND_URL).respond(200, json={"ok": True})
    assert await _notifier().notify_warning("Quelle im Cooldown") is True
    payload = json.loads(route.calls.last.request.read())
    assert "Quelle im Cooldown" in payload["text"]
    assert "reply_markup" not in payload


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

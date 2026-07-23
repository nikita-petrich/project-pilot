"""Tests for the Telegram client and rich message formatting (respx-mocked)."""

import json

import httpx
import respx

from project_pilot.notification.telegram import (
    MatchMessage,
    TelegramClient,
    format_match,
)

API = "https://api.telegram.org/bottok:123/sendMessage"


def _full_message() -> MatchMessage:
    return MatchMessage(
        title="AI Engineer",
        url="https://x/1",
        score=90,
        company="Talent Co",
        contact_name="Harry Schneider",
        is_endcustomer=True,
        location="London, GB",
        remote_label="100% Remote / 0% vor Ort",
        contract_type="Freiberuflich",
        workload_label="80% Auslastung",
        duration_label="6 Mon (+ Verlängerung)",
        start="ab sofort",
        posted_ago="vor 8 Min",
        expires_label="bis 23.10.2026",
        industry="IT",
        language="Englisch",
        skills=["Python", "RAG", "TypeScript"],
        reasons=["LLM/RAG core", "modern TS stack"],
        matching_skills=["Python", "RAG"],
        missing_requirements=["Solidity"],
        risk_flags=["budget unclear"],
        description="Build the future of AI.",
    )


def test_format_match_includes_all_sections() -> None:
    text = format_match(_full_message())
    assert '<a href="https://x/1">' in text
    assert "AI Engineer" in text and "90/100" in text
    assert "Talent Co" in text and "Endkunde" in text
    assert "London, GB" in text and "100% Remote / 0% vor Ort" in text
    assert "Freiberuflich" in text and "80% Auslastung" in text and "Verlängerung" in text
    assert "ab sofort" in text and "vor 8 Min" in text and "bis 23.10.2026" in text
    assert "IT" in text and "Englisch" in text
    assert "Python" in text and "RAG" in text
    assert "LLM/RAG core" in text
    assert "Solidity" in text and "budget unclear" in text
    assert "Build the future of AI." in text
    # research links, url-encoded
    assert "linkedin.com/search/results/people/?keywords=Harry%20Schneider" in text
    assert "google.com/search?q=Talent%20Co" in text


def test_format_match_escapes_html() -> None:
    text = format_match(MatchMessage(title="A <b>& B", url="https://x", score=50))
    assert "&lt;b&gt;" in text
    assert "&amp;" in text


def test_format_match_minimal_omits_absent_fields() -> None:
    text = format_match(MatchMessage(title="T", url="https://x", score=60))
    assert "T" in text and "60/100" in text
    assert "🏢" not in text  # no company line
    assert "📄" not in text  # no description


def test_format_match_caps_description() -> None:
    long_desc = "x" * 5000
    text = format_match(MatchMessage(title="T", url="https://x", score=60, description=long_desc))
    assert "…" in text
    assert len(text) < 4096  # stays within Telegram's message limit


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

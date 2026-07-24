"""Tests for the Telegram client and rich message formatting (respx-mocked)."""

import json

import httpx
import respx

from project_pilot.application.service import DraftView
from project_pilot.models import ApplicationStatus
from project_pilot.notification.telegram import (
    MatchMessage,
    TelegramClient,
    apply_keyboard,
    draft_keyboard,
    email_body_messages,
    format_draft,
    format_match,
)

API = "https://api.telegram.org/bottok:123/sendMessage"


def _full_message() -> MatchMessage:
    return MatchMessage(
        title="AI Engineer",
        url="https://x/1",
        score=90,
        company="Talent Co",
        contact_name="Anna Kleinen",
        is_endcustomer=True,
        location="London, GB",
        remote_label="100%",
        contract_type="Freiberuflich",
        workload_label="80%",
        duration_label="6 Mon (+ Verlängerung)",
        start="ab sofort",
        posted_ago="vor 8 Min",
        expires_label="23.10.2026",
        industry="IT",
        language="Englisch",
        skills=["Python", "RAG", "TypeScript"],
        reasons=["LLM/RAG core", "modern TS stack"],
        matching_skills=["Python", "RAG"],
        missing_requirements=["Solidity"],
        risk_flags=["budget unclear"],
        description="Build the future of AI.",
    )


def test_format_match_includes_all_labeled_fields() -> None:
    text = format_match(_full_message())
    assert '<a href="https://x/1">' in text
    assert "AI Engineer" in text and "90/100" in text
    assert "<b>Firma:</b> Talent Co" in text
    assert "<b>Ansprechpartner:</b>" in text and "Anna Kleinen" in text
    assert "<b>Auftraggeber:</b> Endkunde" in text
    assert "<b>Einsatzort:</b> London, GB" in text
    assert "<b>Remote:</b> 100%" in text
    assert "<b>Beschäftigungsart:</b> Freiberuflich" in text
    assert "<b>Auslastung:</b> 80%" in text
    assert "<b>Dauer:</b> 6 Mon (+ Verlängerung)" in text
    assert "<b>Start:</b> ab sofort" in text
    assert "<b>Eingestellt:</b> vor 8 Min" in text
    assert "<b>Bewerbung bis:</b> 23.10.2026" in text
    assert "<b>Branche:</b> IT" in text and "<b>Sprache:</b> Englisch" in text
    assert "<b>Skills:</b>" in text and "Python" in text
    assert "<b>Passt:</b>" in text and "LLM/RAG core" in text
    assert "<b>Lücken:</b> Solidity" in text and "<b>Risiken:</b> budget unclear" in text
    assert "<b>Beschreibung:</b>" in text and "Build the future of AI." in text
    # research links, url-encoded
    assert "linkedin.com/search/results/people/?keywords=Anna%20Kleinen" in text
    assert "google.com/search?q=Talent%20Co" in text


def test_format_match_escapes_html() -> None:
    text = format_match(MatchMessage(title="A <b>& B", url="https://x", score=50))
    assert "&lt;b&gt;" in text
    assert "&amp;" in text


def test_format_match_minimal_omits_absent_fields() -> None:
    text = format_match(MatchMessage(title="T", url="https://x", score=60))
    assert "T" in text and "60/100" in text
    assert "Firma:" not in text
    assert "Beschreibung:" not in text


def test_format_match_caps_description() -> None:
    long_desc = "x" * 5000
    text = format_match(MatchMessage(title="T", url="https://x", score=60, description=long_desc))
    assert "…" in text
    assert len(text) < 4096  # stays within Telegram's message limit


@respx.mock
async def test_send_message_success() -> None:
    route = respx.post(API).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
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


def _draft_view(
    recipient: str | None = "pm@firma.de",
    status: ApplicationStatus = ApplicationStatus.READY,
    body: str = "Sehr geehrte Damen und Herren,\nich passe.",
) -> DraftView:
    return DraftView(
        application_id=3,
        title="KI-Projekt <live>",
        url="https://x/1",
        recipient=recipient,
        subject="Bewerbung: KI-Projekt",
        body=body,
        linkedin_message="Hallo, kurzes Interesse!",
        status=status,
        revision_count=1,
    )


def test_format_draft_summary_has_metadata_not_body() -> None:
    text = format_draft(_draft_view())
    assert "Bewerbungsentwurf" in text
    assert "KI-Projekt &lt;live&gt;" in text  # HTML-escaped title
    assert "<b>An:</b> pm@firma.de" in text
    assert "<code>Bewerbung: KI-Projekt</code>" in text  # subject in a copy block
    assert "<pre>Hallo, kurzes Interesse!</pre>" in text  # LinkedIn copy block
    # mailto link opens the mail client with recipient + subject prefilled
    assert 'href="mailto:pm@firma.de?subject=Bewerbung' in text
    assert "Überarbeitung #1" in text
    assert "oder tippe Senden" in text
    # the e-mail body is NOT in the summary (it ships in its own message)
    assert "ich passe." not in text


def test_format_draft_without_recipient_omits_mailto_and_asks_for_address() -> None:
    text = format_draft(_draft_view(recipient=None, status=ApplicationStatus.AWAITING_EMAIL))
    assert "❓ unbekannt" in text
    assert "E-Mail-Adresse" in text
    assert "mailto:" not in text  # no recipient → no mail-client link
    assert "oder tippe Senden" not in text


def test_email_body_messages_single_block_when_short() -> None:
    messages = email_body_messages(_draft_view())
    assert len(messages) == 1
    assert "Vollständige E-Mail" in messages[0]
    assert "<pre>Sehr geehrte Damen und Herren,\nich passe.</pre>" in messages[0]
    assert "Teil " not in messages[0]  # no part counter for a single message


def test_email_body_messages_split_long_body_without_truncation() -> None:
    body = "\n".join(f"Zeile {i}: " + "wort " * 40 for i in range(200))
    messages = email_body_messages(_draft_view(body=body))
    assert len(messages) >= 2
    assert all(len(message) <= 4096 for message in messages)  # under Telegram's limit
    assert all("…" not in message for message in messages)  # nothing cut off
    assert "Teil 1/" in messages[0] and f"Teil {len(messages)}/" in messages[-1]
    # every source line survives somewhere across the messages (lossless)
    joined = "".join(messages)
    assert "Zeile 0:" in joined and "Zeile 199:" in joined


def test_keyboards_carry_callback_data() -> None:
    apply = apply_keyboard(7)["inline_keyboard"]
    assert apply == [[{"text": "📝 Bewerben", "callback_data": "apply:7"}]]
    both = draft_keyboard(3, can_send=True)["inline_keyboard"]
    assert isinstance(both, list)
    assert [button["callback_data"] for button in both[0]] == ["send:3", "cancel:3"]
    cancel_only = draft_keyboard(3, can_send=False)["inline_keyboard"]
    assert isinstance(cancel_only, list)
    assert [button["callback_data"] for button in cancel_only[0]] == ["cancel:3"]


@respx.mock
async def test_send_returns_message_id_and_posts_keyboard() -> None:
    route = respx.post(API).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 55}})
    )
    async with TelegramClient(bot_token="tok:123", chat_id="7") as client:
        message_id = await client.send("hi", reply_markup=apply_keyboard(9))
    assert message_id == 55
    payload = json.loads(route.calls.last.request.content)
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "apply:9"


@respx.mock
async def test_send_match_attaches_apply_button() -> None:
    route = respx.post(API).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    async with TelegramClient(bot_token="tok:123", chat_id="7") as client:
        assert await client.send_match("match", listing_id=4)
    payload = json.loads(route.calls.last.request.content)
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "apply:4"


@respx.mock
async def test_answer_callback_posts_query_id() -> None:
    route = respx.post("https://api.telegram.org/bottok:123/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    async with TelegramClient(bot_token="tok:123", chat_id="7") as client:
        assert await client.answer_callback("cb9", "Moment …")
    payload = json.loads(route.calls.last.request.content)
    assert payload["callback_query_id"] == "cb9"
    assert payload["text"] == "Moment …"


@respx.mock
async def test_get_updates_parses_messages_and_callbacks() -> None:
    respx.post("https://api.telegram.org/bottok:123/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 5,
                        "message": {
                            "message_id": 1,
                            "text": "/apply x",
                            "chat": {"id": 7, "type": "private"},
                            "reply_to_message": {"message_id": 9, "chat": {"id": 7}},
                        },
                    },
                    {
                        "update_id": 6,
                        "callback_query": {
                            "id": "cb",
                            "data": "apply:3",
                            "message": {"message_id": 2, "chat": {"id": 7}},
                        },
                    },
                    {"update_id": "broken"},
                    {"update_id": 7, "message": {"bad": True}},
                ],
            },
        )
    )
    async with TelegramClient(bot_token="tok:123", chat_id="7") as client:
        updates = await client.get_updates(offset=None)
    assert updates is not None
    # id 7 is unparsable but still advances the offset (as an empty placeholder)
    assert [update.update_id for update in updates] == [5, 6, 7]
    assert updates[2].message is None
    assert updates[2].callback_query is None
    first, second = updates[0], updates[1]
    assert first.message is not None
    assert first.message.reply_to_message is not None
    assert first.message.reply_to_message.message_id == 9
    assert second.callback_query is not None
    assert second.callback_query.data == "apply:3"


@respx.mock
async def test_get_updates_returns_none_on_server_error() -> None:
    respx.post("https://api.telegram.org/bottok:123/getUpdates").mock(
        return_value=httpx.Response(502)
    )
    async with TelegramClient(bot_token="tok:123", chat_id="7") as client:
        assert await client.get_updates(offset=1) is None

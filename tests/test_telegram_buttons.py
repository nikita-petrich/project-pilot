"""The button handler: parsing, the whitelist, accept drafts, decline deletes."""

import json

import httpx
import respx

from project_pilot.application.service import DraftView
from project_pilot.errors import ApplicationStateError
from project_pilot.models import ApplicationStatus
from project_pilot.telegram_bot import (
    Callback,
    TelegramButtons,
    accepted_text,
    parse_callbacks,
)

BOT_TOKEN = "123456:AAtest"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CHAT_ID = "-1001234567890"
PROJECT_URL = "https://claude.ai/cowork/project/01a0"
ME = 4242
MESSAGE_ID = 900


def _draft(application_id: int = 12, recipient: str | None = "paul@acme.test") -> DraftView:
    return DraftView(
        application_id=application_id,
        title="Senior Python Developer",
        url="https://example.com/p/1",
        contact_name="Paul",
        recipient=recipient,
        subject="Bewerbung als Senior Python Developer",
        body="Guten Tag …",
        linkedin_message="Hallo Paul",
        status=ApplicationStatus.READY,
        revision_count=0,
        listing_id=42,
    )


class _FakeDrafter:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[int] = []

    async def draft_for_listing(self, listing_id: int) -> DraftView:
        self.calls.append(listing_id)
        if self.error is not None:
            raise self.error
        return _draft()


def _press(
    update_id: int = 1, *, data: str = "accept:42", user_id: int = ME, chat_id: str = CHAT_ID
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": MESSAGE_ID, "chat": {"id": int(chat_id)}},
        },
    }


def _buttons(drafter: _FakeDrafter, *, project_url: str = PROJECT_URL) -> TelegramButtons:
    return TelegramButtons(
        bot_token=BOT_TOKEN,
        chat_id=CHAT_ID,
        allowed_user_ids=[ME],
        drafter=drafter,
        project_url=project_url,
    )


def test_parse_callbacks_reads_the_action_and_its_listing() -> None:
    parsed = parse_callbacks({"result": [_press(3, data="decline:534")]})
    assert parsed == [
        Callback(
            update_id=3,
            callback_id="cb3",
            chat_id=int(CHAT_ID),
            message_id=MESSAGE_ID,
            user_id=ME,
            action="decline",
            listing_id=534,
        )
    ]


def test_parse_callbacks_drops_anything_it_cannot_act_on() -> None:
    payload = {
        "result": [
            _press(1, data="explode:42"),  # unknown action
            _press(2, data="accept:notanumber"),
            _press(3, data="accept"),  # no listing id
            {"update_id": 4, "message": {"text": "a normal message"}},
        ]
    }
    assert parse_callbacks(payload) == []
    assert parse_callbacks({}) == []
    assert parse_callbacks({"result": "nonsense"}) == []


@respx.mock
async def test_decline_deletes_the_card() -> None:
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_press(1, data="decline:42")]})
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    delete = respx.post(f"{API}/deleteMessage").respond(200, json={"ok": True})

    drafter = _FakeDrafter()
    async with httpx.AsyncClient() as client:
        assert await _buttons(drafter).poll_once(client) == 1

    assert drafter.calls == []  # declining never drafts
    assert json.loads(delete.calls.last.request.read())["message_id"] == MESSAGE_ID


@respx.mock
async def test_accept_drafts_and_rewrites_the_card() -> None:
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_press(1)]})
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    edit = respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})

    drafter = _FakeDrafter()
    async with httpx.AsyncClient() as client:
        assert await _buttons(drafter).poll_once(client) == 1

    assert drafter.calls == [42]
    payload = json.loads(edit.calls.last.request.read())
    assert payload["message_id"] == MESSAGE_ID
    assert "Bewerbung 12" in payload["text"]
    # Only one button is left, and it leads where the work continues.
    assert payload["reply_markup"]["inline_keyboard"] == [
        [{"text": "💬 In Claude öffnen", "url": PROJECT_URL}]
    ]


@respx.mock
async def test_a_failed_draft_says_so_instead_of_going_silent() -> None:
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_press(1)]})
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    edit = respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})

    drafter = _FakeDrafter(error=ApplicationStateError("Project 42 not found"))
    async with httpx.AsyncClient() as client:
        assert await _buttons(drafter).poll_once(client) == 0

    assert "fehlgeschlagen" in json.loads(edit.calls.last.request.read())["text"]


@respx.mock
async def test_a_stranger_cannot_press_anything() -> None:
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_press(1, user_id=999)]})
    answer = respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    delete = respx.post(f"{API}/deleteMessage").respond(200, json={"ok": True})

    drafter = _FakeDrafter()
    async with httpx.AsyncClient() as client:
        assert await _buttons(drafter).poll_once(client) == 0

    assert drafter.calls == []
    assert delete.call_count == 0
    assert "Nicht berechtigt" in json.loads(answer.calls.last.request.read())["text"]


@respx.mock
async def test_a_press_from_another_chat_is_ignored() -> None:
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_press(1, chat_id="-1009999999999")]}
    )
    drafter = _FakeDrafter()
    async with httpx.AsyncClient() as client:
        assert await _buttons(drafter).poll_once(client) == 0
    assert drafter.calls == []


@respx.mock
async def test_only_callback_updates_are_requested() -> None:
    # The bot holds no conversation, so nothing else is worth receiving.
    route = respx.post(f"{API}/getUpdates").respond(200, json={"result": []})
    async with httpx.AsyncClient() as client:
        await _buttons(_FakeDrafter()).poll_once(client)
    assert json.loads(route.calls.last.request.read())["allowed_updates"] == ["callback_query"]


@respx.mock
async def test_the_offset_advances_past_dropped_presses() -> None:
    # An unacknowledged update is redelivered forever.
    route = respx.post(f"{API}/getUpdates").respond(200, json={"result": [_press(7, user_id=999)]})
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    buttons = _buttons(_FakeDrafter())
    async with httpx.AsyncClient() as client:
        await buttons.poll_once(client)
        await buttons.poll_once(client)

    assert json.loads(route.calls.last.request.read())["offset"] == 8


def test_accepted_text_names_the_ids_the_next_step_needs() -> None:
    # The Claude chat it points at starts empty; these ids are what gets typed.
    text = accepted_text(_draft())
    assert "/write-application 42" in text
    assert "/send-application 12" in text
    assert "paul@acme.test" in text


def test_accepted_text_flags_a_missing_recipient() -> None:
    assert "Kein Empfänger erkannt" in accepted_text(_draft(recipient=None))

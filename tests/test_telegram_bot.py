"""The decline poller: parsing, chat guard, delete with fallback, offset."""

import json

import httpx
import respx

from project_pilot.telegram_bot import TelegramButtons, parse_presses, update_ids

BOT_TOKEN = "123456:AAtest-token"
CHAT_ID = "987654321"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _press(
    *,
    update_id: int = 7,
    chat_id: str = CHAT_ID,
    data: str = "decline:42",
    message_id: int = 5150,
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": 1},
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": int(chat_id)},
                "text": "⭐ 87 · Senior Python Developer · ACME GmbH",
            },
        },
    }


def _updates(*items: dict[str, object]) -> dict[str, object]:
    return {"ok": True, "result": list(items)}


def test_parse_presses_reads_the_listing_id_off_the_callback() -> None:
    (press,) = parse_presses(_updates(_press()))
    assert press.action == "decline"
    assert press.listing_id == 42
    assert press.message_id == 5150
    assert press.chat_id == CHAT_ID
    assert press.callback_id == "cb7"


def test_parse_presses_skips_anything_that_is_not_a_press() -> None:
    payload = _updates({"update_id": 1, "message": {"text": "hi"}}, {"update_id": 2}, _press())
    assert len(parse_presses(payload)) == 1
    assert update_ids(payload) == [1, 2, 7]


async def _poll(bot: TelegramButtons) -> int:
    async with httpx.AsyncClient() as client:
        return await bot.poll_once(client)


@respx.mock
async def test_decline_deletes_the_card_and_answers_the_press() -> None:
    respx.post(f"{API}/getUpdates").respond(200, json=_updates(_press()))
    delete = respx.post(f"{API}/deleteMessage").respond(200, json={"ok": True, "result": True})
    answer = respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    edit = respx.post(f"{API}/editMessageText")

    bot = TelegramButtons(bot_token=BOT_TOKEN, chat_id=CHAT_ID)
    assert await _poll(bot) == 1

    payload = json.loads(delete.calls.last.request.read())
    assert payload == {"chat_id": CHAT_ID, "message_id": 5150}
    assert answer.called
    assert not edit.called


@respx.mock
async def test_a_card_too_old_to_delete_is_stripped_and_marked_instead() -> None:
    # Telegram refuses to delete a bot message after 48 hours; the press must
    # still visibly take the match off the table.
    respx.post(f"{API}/getUpdates").respond(200, json=_updates(_press()))
    respx.post(f"{API}/deleteMessage").respond(
        400, json={"ok": False, "description": "message can't be deleted"}
    )
    edit = respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})

    assert await _poll(TelegramButtons(bot_token=BOT_TOKEN, chat_id=CHAT_ID)) == 1
    payload = json.loads(edit.calls.last.request.read())
    assert payload["text"].startswith("🚫 Abgelehnt")
    assert "reply_markup" not in payload  # the buttons are gone with the edit


@respx.mock
async def test_a_press_from_another_chat_is_ignored_but_the_offset_moves_on() -> None:
    updates = respx.post(f"{API}/getUpdates")
    updates.side_effect = [
        httpx.Response(200, json=_updates(_press(update_id=9, chat_id="111"))),
        httpx.Response(200, json=_updates()),
    ]
    delete = respx.post(f"{API}/deleteMessage")

    bot = TelegramButtons(bot_token=BOT_TOKEN, chat_id=CHAT_ID)
    assert await _poll(bot) == 0
    assert not delete.called
    await _poll(bot)
    # The foreign press is acknowledged as seen, so Telegram never re-serves it.
    assert json.loads(updates.calls.last.request.read())["offset"] == 10


@respx.mock
async def test_polling_asks_for_button_presses_only() -> None:
    updates = respx.post(f"{API}/getUpdates").respond(200, json=_updates())
    await _poll(TelegramButtons(bot_token=BOT_TOKEN, chat_id=CHAT_ID))
    payload = json.loads(updates.calls.last.request.read())
    assert payload["allowed_updates"] == ["callback_query"]
    assert "offset" not in payload  # nothing seen yet


@respx.mock
async def test_an_unknown_action_is_answered_and_not_deleted() -> None:
    respx.post(f"{API}/getUpdates").respond(200, json=_updates(_press(data="accept:42")))
    delete = respx.post(f"{API}/deleteMessage")
    answer = respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})

    assert await _poll(TelegramButtons(bot_token=BOT_TOKEN, chat_id=CHAT_ID)) == 0
    assert not delete.called
    assert answer.called

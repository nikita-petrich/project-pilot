"""The bot process: hears the one button that needs hearing, **Ablehnen**.

Long polling, not a webhook: the worker keeps no inbound port, exactly as the
notification side does. ``getUpdates`` blocks on Telegram's side until something
arrives or the timeout expires, so an idle bot costs one open connection and
nothing else — and it asks for ``callback_query`` updates only, because that is
all it acts on.

This is deliberately all the process does. Bewerben and the listing are URL
buttons that open on their own; the conversation about a match happens in its
Claude session, not here. No agent, no database, no history: a press names the
message it sits on, and deleting that message is the whole job. The chat id is
the only guard it needs — the card lives in the private chat with the bot, so a
press from any other chat is not one of ours.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from project_pilot.notification.telegram import API_BASE, DECLINE_ACTION

logger = logging.getLogger(__name__)

# Telegram holds the request open this long when nothing is happening.
POLL_TIMEOUT_S = 50
_HTTP_TIMEOUT = POLL_TIMEOUT_S + 15
# How long to wait after a failed poll before asking again.
RETRY_DELAY_S = 5.0
DECLINED = "🚫 Abgelehnt"


@dataclass(frozen=True, slots=True)
class Press:
    """One button press: who answered, on which message, and what it asked for."""

    callback_id: str
    chat_id: str
    message_id: int
    action: str
    listing_id: int | None
    text: str


def update_ids(payload: Mapping[str, object]) -> list[int]:
    """Every update id in a ``getUpdates`` result, so the offset can move past them all."""
    result = payload.get("result")
    if not isinstance(result, list):
        return []
    ids = [item.get("update_id") for item in result if isinstance(item, dict)]
    return [item for item in ids if isinstance(item, int)]


def parse_presses(payload: Mapping[str, object]) -> list[Press]:
    """The button presses in a ``getUpdates`` result; anything malformed is skipped."""
    result = payload.get("result")
    if not isinstance(result, list):
        return []
    presses: list[Press] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        query = item.get("callback_query")
        if not isinstance(query, dict):
            continue
        message = query.get("message")
        data = query.get("data")
        callback_id = query.get("id")
        if not (
            isinstance(message, dict) and isinstance(data, str) and isinstance(callback_id, str)
        ):
            continue
        chat = message.get("chat")
        message_id = message.get("message_id")
        if not (isinstance(chat, dict) and isinstance(message_id, int)):
            continue
        action, _, raw_id = data.partition(":")
        listing_id = int(raw_id) if raw_id.isdigit() else None
        text = message.get("text")
        presses.append(
            Press(
                callback_id=callback_id,
                chat_id=str(chat.get("id")),
                message_id=message_id,
                action=action,
                listing_id=listing_id,
                text=text if isinstance(text, str) else "",
            )
        )
    return presses


class TelegramButtons:
    """Polls for button presses on the match cards and acts on **Ablehnen**."""

    def __init__(self, *, bot_token: str, chat_id: str) -> None:
        self._api = f"{API_BASE}/bot{bot_token}"
        self._chat_id = chat_id
        self._offset: int | None = None

    async def run_forever(self) -> None:
        """Poll until cancelled; a failed poll is logged and retried, never fatal."""
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            while True:
                try:
                    await self.poll_once(client)
                except httpx.HTTPError as err:
                    logger.warning("telegram poll failed: %s", err)
                    await asyncio.sleep(RETRY_DELAY_S)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        """One ``getUpdates`` round; the number of presses handled.

        The offset moves past every update in the batch, handled or not, so a
        press from a foreign chat can never wedge the loop by being re-served.
        """
        params: dict[str, object] = {
            "timeout": POLL_TIMEOUT_S,
            "allowed_updates": ["callback_query"],
        }
        if self._offset is not None:
            params["offset"] = self._offset
        response = await client.post(f"{self._api}/getUpdates", json=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return 0
        ids = update_ids(payload)
        if ids:
            self._offset = max(ids) + 1
        handled = 0
        for press in parse_presses(payload):
            if await self._handle(client, press):
                handled += 1
        return handled

    async def _handle(self, client: httpx.AsyncClient, press: Press) -> bool:
        if press.chat_id != self._chat_id:
            logger.warning("ignoring a press from chat %s", press.chat_id)
            return False
        if press.action != DECLINE_ACTION:
            await self._answer(client, press.callback_id, "Unbekannte Aktion")
            return False
        await self._decline(client, press)
        return True

    async def _decline(self, client: httpx.AsyncClient, press: Press) -> None:
        """Take the card off the feed: delete it, or strip it when Telegram refuses.

        A bot may only delete a message for 48 hours. Past that, the fallback
        removes the buttons and marks the card declined, so the press still
        visibly did something.
        """
        deleted = await self._call(
            client, "deleteMessage", {"chat_id": press.chat_id, "message_id": press.message_id}
        )
        if not deleted:
            await self._call(
                client,
                "editMessageText",
                {
                    "chat_id": press.chat_id,
                    "message_id": press.message_id,
                    "text": f"{DECLINED}\n\n{press.text}"[:4_000],
                    "disable_web_page_preview": True,
                },
            )
        await self._answer(client, press.callback_id, "Abgelehnt")
        logger.info("declined listing %s (message %s)", press.listing_id, press.message_id)

    async def _answer(self, client: httpx.AsyncClient, callback_id: str, text: str) -> None:
        """Stop the button's spinner; Telegram shows the text as a toast."""
        await self._call(
            client, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text}
        )

    async def _call(
        self, client: httpx.AsyncClient, method: str, payload: dict[str, object]
    ) -> bool:
        """One Bot API call; False on any failure, which is logged and not raised."""
        try:
            response = await client.post(f"{self._api}/{method}", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as err:
            logger.warning("telegram %s failed: %s", method, err)
            return False
        body = response.json()
        return isinstance(body, dict) and body.get("ok") is True

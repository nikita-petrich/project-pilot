"""The button handler behind the match cards.

Every match arrives in Telegram as one card with three buttons. Two of them do
something here:

* **Abnehmen** deletes the card. A rejected project should leave no trace in the
  feed, and the database keeps the record either way.
* **Annehmen** drafts the application right away — through the same
  ``ApplicationService`` the MCP tools use, so there is one drafting path, not
  two — and then rewrites the card into a pointer at the Claude project, where
  the draft is reviewed and finished with the skills.

The third button is a plain URL to the listing and needs no code.

Long polling, so the worker still publishes no port. Only ``callback_query``
updates are read; the bot holds no conversation and answers no messages, which
is what keeps this process small.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from project_pilot.application.service import DraftView
from project_pilot.errors import ApplicationStateError

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_S = 50
_HTTP_TIMEOUT = POLL_TIMEOUT_S + 15

ACCEPT = "accept"
DECLINE = "decline"


class Drafter(Protocol):
    """The ``ApplicationService`` subset the accept button needs."""

    async def draft_for_listing(self, listing_id: int) -> DraftView: ...


@dataclass(frozen=True, slots=True)
class Callback:
    """One button press, narrowed to what acting on it needs."""

    update_id: int
    callback_id: str
    chat_id: int
    message_id: int
    user_id: int
    action: str
    listing_id: int


def parse_callbacks(payload: Mapping[str, object]) -> list[Callback]:
    """Pick the button presses out of a getUpdates response.

    The callback data is ``<action>:<listing_id>`` — the listing id travels with
    every press rather than being looked up from some "current" state, so two
    cards can never be confused for one another.
    """
    result = payload.get("result")
    if not isinstance(result, list):
        return []
    presses: list[Callback] = []
    for update in result:
        if not isinstance(update, dict):
            continue
        update_id, query = update.get("update_id"), update.get("callback_query")
        if not isinstance(update_id, int) or not isinstance(query, dict):
            continue
        data, sender, message = query.get("data"), query.get("from"), query.get("message")
        callback_id = query.get("id")
        if not isinstance(data, str) or not isinstance(callback_id, str):
            continue
        if not isinstance(sender, dict) or not isinstance(message, dict):
            continue
        action, _, raw_id = data.partition(":")
        chat = message.get("chat")
        message_id, user_id = message.get("message_id"), sender.get("id")
        if action not in {ACCEPT, DECLINE} or not raw_id.isdigit():
            continue
        if not isinstance(chat, dict) or not isinstance(message_id, int):
            continue
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            continue
        presses.append(
            Callback(
                update_id=update_id,
                callback_id=callback_id,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                action=action,
                listing_id=int(raw_id),
            )
        )
    return presses


class TelegramButtons:
    """Polls for button presses on the match cards and acts on them."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        allowed_user_ids: Sequence[int],
        drafter: Drafter,
        project_url: str = "",
    ) -> None:
        self._api = f"{API_BASE}/bot{bot_token}"
        self._chat_id = str(chat_id)
        self._allowed = set(allowed_user_ids)
        self._drafter = drafter
        self._project_url = project_url.strip()
        self._offset = 0

    async def run_forever(self) -> None:
        """Poll until cancelled, surviving transient Telegram failures."""
        logger.info("telegram buttons started; allowed users: %s", sorted(self._allowed))
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            while True:
                try:
                    await self.poll_once(client)
                except httpx.HTTPError as err:
                    logger.warning("polling failed, retrying: %s", err)
                    await asyncio.sleep(5)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        """One getUpdates round; returns how many presses were acted on."""
        response = await client.post(
            f"{self._api}/getUpdates",
            json={
                "offset": self._offset,
                "timeout": POLL_TIMEOUT_S,
                # Nothing else is acted on, so nothing else is worth receiving.
                "allowed_updates": ["callback_query"],
            },
        )
        response.raise_for_status()
        payload = response.json()
        handled = 0
        for press in parse_callbacks(payload if isinstance(payload, dict) else {}):
            # Advance past every update, including dropped ones: an update left
            # unacknowledged is redelivered forever.
            self._offset = max(self._offset, press.update_id + 1)
            if await self._handle(client, press):
                handled += 1
        return handled

    async def _handle(self, client: httpx.AsyncClient, press: Callback) -> bool:
        if self._allowed and press.user_id not in self._allowed:
            logger.warning("ignoring button press from user %s", press.user_id)
            await self._answer(client, press.callback_id, "Nicht berechtigt.")
            return False
        if str(press.chat_id) != self._chat_id:
            logger.warning("ignoring button press from chat %s", press.chat_id)
            return False

        if press.action == DECLINE:
            # Telegram shows a spinner on the button until the query is answered.
            await self._answer(client, press.callback_id, "Abgelehnt.")
            await self._delete(client, press.message_id)
            logger.info("declined listing %s", press.listing_id)
            return True

        await self._answer(client, press.callback_id, "Bewerbung wird geschrieben …")
        try:
            draft = await self._drafter.draft_for_listing(press.listing_id)
        except (ApplicationStateError, OSError) as err:
            logger.warning("drafting failed for listing %s: %s", press.listing_id, err)
            await self._edit(
                client,
                press.message_id,
                f"⚠️ Bewerbung für Listing {press.listing_id} fehlgeschlagen: {err}",
                self._open_keyboard(),
            )
            return False
        logger.info("accepted listing %s, application %s", press.listing_id, draft.application_id)
        await self._edit(
            client,
            press.message_id,
            accepted_text(draft),
            self._open_keyboard(),
        )
        return True

    def _open_keyboard(self) -> dict[str, object] | None:
        """The single button left after a decision: open the Claude project."""
        if not self._project_url:
            return None
        return {"inline_keyboard": [[{"text": "💬 In Claude öffnen", "url": self._project_url}]]}

    async def _answer(self, client: httpx.AsyncClient, callback_id: str, text: str) -> None:
        try:
            await client.post(
                f"{self._api}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
            )
        except httpx.HTTPError as err:  # the spinner clears on its own after a while
            logger.debug("answering the callback failed: %s", err)

    async def _delete(self, client: httpx.AsyncClient, message_id: int) -> None:
        try:
            response = await client.post(
                f"{self._api}/deleteMessage",
                json={"chat_id": self._chat_id, "message_id": message_id},
            )
            response.raise_for_status()
        except httpx.HTTPError as err:
            logger.warning("deleting message %s failed: %s", message_id, err)

    async def _edit(
        self,
        client: httpx.AsyncClient,
        message_id: int,
        text: str,
        keyboard: dict[str, object] | None,
    ) -> None:
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            response = await client.post(f"{self._api}/editMessageText", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as err:
            logger.warning("editing message %s failed: %s", message_id, err)


def accepted_text(draft: DraftView) -> str:
    """What the card becomes once the draft exists.

    Names the ids the next step needs, because the Claude chat it points at
    starts empty and those ids are what gets typed there.
    """
    lines = [
        f"✅ Angenommen · {draft.title}",
        "",
        f"Bewerbung {draft.application_id} ist geschrieben.",
    ]
    if draft.recipient:
        lines.append(f"Empfänger erkannt: {draft.recipient}")
    else:
        lines.append("Kein Empfänger erkannt — im Chat setzen.")
    lines += [
        "",
        f"Betreff: {draft.subject}",
        "",
        "Im Claude-Projekt weiterarbeiten:",
        f"  /write-application {draft.listing_id}   (Entwurf ansehen und ändern)",
        f"  /send-application {draft.application_id}   (nach deiner Bestätigung senden)",
    ]
    return "\n".join(lines)

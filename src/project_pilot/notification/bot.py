"""Telegram long-poll bot: Apply buttons, ``/apply`` command, draft review replies.

Only the configured chat is served; every state change is guarded in the
service layer, so replayed or duplicated updates cannot double-send an e-mail.
"""

import asyncio
import contextlib
import html
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Protocol

from project_pilot.application.service import DraftView, is_email
from project_pilot.errors import ApplicationStateError, EmailSendError, LlmSchemaError
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import ApplicationStatus
from project_pilot.notification.telegram import (
    TelegramCallbackQuery,
    TelegramMessage,
    TelegramUpdate,
    draft_keyboard,
    format_draft,
)

logger = logging.getLogger(__name__)

_APPLY_RE = re.compile(r"^/apply(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)
_HELP_RE = re.compile(r"^/(start|help)(?:@\w+)?\b")

HELP_TEXT = (
    "🤖 <b>project-pilot</b>\n\n"
    "📝 Tippe <b>Bewerben</b> unter einer Match-Nachricht, um einen "
    "Bewerbungsentwurf zu erstellen.\n"
    "⌨️ <code>/apply &lt;Link oder Projektbeschreibung&gt;</code> startet den "
    "gleichen Flow für ein beliebiges Projekt.\n"
    "✏️ Antworte auf einen Entwurf, um Änderungen zu beschreiben - oder mit "
    "einer E-Mail-Adresse, um den Empfänger zu setzen.\n"
    "📤 Erst <b>Senden</b> verschickt die E-Mail wirklich."
)


class BotClient(Protocol):
    """The Telegram client surface the bot needs (fakeable in tests)."""

    async def get_updates(
        self, *, offset: int | None = None, timeout_s: int = 25
    ) -> list[TelegramUpdate] | None: ...

    async def send(
        self,
        text: str,
        *,
        disable_preview: bool = True,
        reply_markup: dict[str, object] | None = None,
    ) -> int | None: ...

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> bool: ...


class ApplicationFlow(Protocol):
    """The application-service surface the bot drives."""

    async def draft_for_listing(self, listing_id: int) -> DraftView: ...
    async def draft_from_parsed(self, parsed: ParsedListing) -> DraftView: ...
    async def draft_from_text(self, text: str) -> DraftView: ...
    async def revise(self, application_id: int, instruction: str) -> DraftView: ...
    async def set_recipient(self, application_id: int, email: str) -> DraftView: ...
    async def send(self, application_id: int) -> DraftView: ...
    async def cancel(self, application_id: int) -> DraftView: ...
    async def record_draft_message(self, application_id: int, message_id: int) -> None: ...
    async def find_by_draft_message(self, message_id: int) -> DraftView | None: ...
    async def find_listing_id_by_url(self, url: str) -> int | None: ...


type ListingFetcher = Callable[[str], Awaitable[ParsedListing]]


class TelegramBot:
    """Routes updates from the configured chat into the application flow."""

    def __init__(
        self,
        *,
        client: BotClient,
        chat_id: str,
        service: ApplicationFlow,
        fetcher: ListingFetcher | None = None,
        poll_timeout_s: int = 25,
        error_backoff_s: float = 5.0,
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._service = service
        self._fetcher = fetcher
        self._poll_timeout_s = poll_timeout_s
        self._error_backoff_s = error_backoff_s
        # Reply routing for older renders of a draft; the newest id lives in the DB.
        self._draft_messages: dict[int, int] = {}

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Long-poll until ``stop`` is set (checked between polls, ≤ one poll window late)."""
        logger.info("telegram bot started (long polling)")
        offset: int | None = None
        while stop is None or not stop.is_set():
            updates = await self._client.get_updates(offset=offset, timeout_s=self._poll_timeout_s)
            if updates is None:
                await asyncio.sleep(self._error_backoff_s)
                continue
            for update in updates:
                offset = update.update_id + 1
                try:
                    await self.process_update(update)
                except Exception:
                    logger.exception("failed to process update %d", update.update_id)
        if offset is not None:  # best effort: confirm the last batch so a restart won't replay it
            with contextlib.suppress(Exception):
                await self._client.get_updates(offset=offset, timeout_s=0)
        logger.info("telegram bot stopped")

    async def process_update(self, update: TelegramUpdate) -> None:
        if update.callback_query is not None:
            await self._handle_callback(update.callback_query)
        elif update.message is not None:
            await self._handle_message(update.message)

    def _is_own_chat(self, message: TelegramMessage) -> bool:
        return str(message.chat.id) == self._chat_id

    async def _handle_callback(self, query: TelegramCallbackQuery) -> None:
        action, _, raw_id = (query.data or "").partition(":")
        if query.message is None or not self._is_own_chat(query.message) or not raw_id.isdigit():
            await self._client.answer_callback(query.id)
            return
        target_id = int(raw_id)
        if action == "apply":
            await self._client.answer_callback(query.id, "Erstelle Entwurf …")
            await self._start_draft(lambda: self._service.draft_for_listing(target_id))
        elif action == "send":
            await self._client.answer_callback(query.id)
            await self._send_application(target_id)
        elif action == "cancel":
            await self._client.answer_callback(query.id)
            await self._cancel_application(target_id)
        else:
            await self._client.answer_callback(query.id)

    async def _handle_message(self, message: TelegramMessage) -> None:
        if not self._is_own_chat(message) or not message.text:
            return
        text = message.text.strip()
        if message.reply_to_message is not None:
            await self._handle_reply(message.reply_to_message.message_id, text)
            return
        apply_match = _APPLY_RE.match(text)
        if apply_match:
            argument = (apply_match.group(1) or "").strip()
            if argument:
                await self._handle_apply(argument)
            else:
                await self._client.send(
                    "Nutzung: <code>/apply &lt;Link oder Projektbeschreibung&gt;</code>"
                )
            return
        if _HELP_RE.match(text):
            await self._client.send(HELP_TEXT)
            return
        await self._client.send(
            "Ich habe dazu keinen Kontext. Antworte direkt auf einen Entwurf oder "
            "nutze <code>/apply</code> - Details mit /help."
        )

    async def _handle_apply(self, argument: str) -> None:
        if not argument.lower().startswith(("http://", "https://")):
            await self._start_draft(lambda: self._service.draft_from_text(argument))
            return
        listing_id = await self._service.find_listing_id_by_url(argument)
        if listing_id is not None:
            await self._start_draft(lambda: self._service.draft_for_listing(listing_id))
            return
        if "freelancermap." not in argument or self._fetcher is None:
            await self._client.send(
                "⚠️ Diesen Link kenne ich nicht. Schicke "
                "<code>/apply</code> mit der Projektbeschreibung als Text."
            )
            return
        fetcher = self._fetcher
        await self._client.send("⏳ Lade das Projekt …")

        async def fetch_and_draft() -> DraftView:
            parsed = await fetcher(argument)
            return await self._service.draft_from_parsed(parsed)

        await self._start_draft(fetch_and_draft)

    async def _handle_reply(self, replied_message_id: int, text: str) -> None:
        view = await self._service.find_by_draft_message(replied_message_id)
        application_id = (
            view.application_id
            if view is not None
            else self._draft_messages.get(replied_message_id)
        )
        if application_id is None:
            await self._client.send(
                "Zu dieser Nachricht finde ich keinen Bewerbungsentwurf. "
                "Antworte direkt auf die Entwurfs-Nachricht."
            )
            return
        if is_email(text):
            await self._show_result(lambda: self._service.set_recipient(application_id, text))
        else:
            await self._client.send("✏️ Überarbeite den Entwurf …")
            await self._show_result(lambda: self._service.revise(application_id, text))

    async def _start_draft(self, factory: Callable[[], Awaitable[DraftView]]) -> None:
        await self._show_result(factory)

    async def _show_result(self, action: Callable[[], Awaitable[DraftView]]) -> None:
        try:
            view = await action()
        except (ApplicationStateError, LlmSchemaError) as err:
            await self._client.send(f"⚠️ {html.escape(str(err))}")
            return
        except Exception as err:
            logger.exception("application action failed")
            await self._client.send(f"⚠️ Unerwarteter Fehler: {html.escape(str(err))}")
            return
        await self._show_draft(view)

    async def _show_draft(self, view: DraftView) -> None:
        keyboard = None
        if view.status in (ApplicationStatus.READY, ApplicationStatus.AWAITING_EMAIL):
            keyboard = draft_keyboard(view.application_id, can_send=view.recipient is not None)
        message_id = await self._client.send(format_draft(view), reply_markup=keyboard)
        if message_id is not None:
            self._remember_draft_message(message_id, view.application_id)
            await self._service.record_draft_message(view.application_id, message_id)
        else:
            logger.warning("draft message for application %d not sent", view.application_id)
            await self._client.send(
                "⚠️ Der Entwurf konnte nicht angezeigt werden (Telegram-Fehler). "
                "Er ist gespeichert - starte den Flow einfach erneut."
            )

    def _remember_draft_message(self, message_id: int, application_id: int) -> None:
        self._draft_messages[message_id] = application_id
        while len(self._draft_messages) > 500:  # bounded memory over long uptimes
            self._draft_messages.pop(next(iter(self._draft_messages)))

    async def _send_application(self, application_id: int) -> None:
        try:
            view = await self._service.send(application_id)
        except (ApplicationStateError, EmailSendError) as err:
            await self._client.send(f"⚠️ {html.escape(str(err))}")
            return
        except Exception as err:
            logger.exception("sending application %d failed", application_id)
            await self._client.send(f"⚠️ Unerwarteter Fehler: {html.escape(str(err))}")
            return
        recipient = html.escape(view.recipient or "")
        await self._client.send(
            f"✅ Bewerbung verschickt an <b>{recipient}</b>\n"
            f"✉️ {html.escape(view.subject)}\n\n"
            f"💬 LinkedIn-Nachricht nicht vergessen - zum Kopieren:\n"
            f"<pre>{html.escape(view.linkedin_message)}</pre>"
        )

    async def _cancel_application(self, application_id: int) -> None:
        try:
            view = await self._service.cancel(application_id)
        except ApplicationStateError as err:
            await self._client.send(f"⚠️ {html.escape(str(err))}")
            return
        await self._client.send(f"❌ Entwurf verworfen: {html.escape(view.title)}")

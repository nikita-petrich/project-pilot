"""Lean Telegram Bot API client (sendMessage, getUpdates, callbacks) over httpx."""

import html
import logging
from typing import Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from project_pilot.application.schemas import LINKEDIN_LIMIT
from project_pilot.application.service import DraftView
from project_pilot.models import ApplicationStatus
from project_pilot.notification.messages import MatchMessage

__all__ = [
    "MatchMessage",
    "TelegramClient",
    "TelegramUpdate",
    "apply_keyboard",
    "draft_keyboard",
    "email_body_messages",
    "format_draft",
    "format_match",
]

logger = logging.getLogger(__name__)

_DESCRIPTION_LIMIT = 2500
# Headroom under Telegram's hard 4096-char message limit; the full e-mail body is
# split across this many visible chars per message so it is never truncated.
_TELEGRAM_TEXT_LIMIT = 3500


class TelegramChat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int


class TelegramMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: int
    text: str | None = None
    chat: TelegramChat
    reply_to_message: "TelegramMessage | None" = None


class TelegramCallbackQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    data: str | None = None
    message: TelegramMessage | None = None


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None


TelegramMessage.model_rebuild()


def _esc(text: str) -> str:
    return html.escape(text)


def _link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{_esc(label)}</a>'


def _search_link(url_prefix: str, query: str, label: str) -> str:
    return _link(f"{url_prefix}{quote(query)}", label)


def _labeled(emoji: str, label: str, value: str | None) -> str | None:
    return f"{emoji} <b>{label}:</b> {_esc(value)}" if value else None


def _labeled_list(emoji: str, label: str, values: list[str], *, limit: int) -> str | None:
    picked = [value for value in values if value][:limit]
    return _labeled(emoji, label, ", ".join(picked)) if picked else None


_LINKEDIN_PEOPLE = "https://www.linkedin.com/search/results/people/?keywords="
_GOOGLE = "https://www.google.com/search?q="


def format_match(message: MatchMessage) -> str:
    """Render one match as a rich, fully labeled single Telegram HTML message."""
    lines: list[str] = [f"🎯 <b>{_esc(message.title)}</b> · {message.score}/100", ""]

    def add(line: str | None) -> None:
        if line:
            lines.append(line)

    add(_labeled("🏢", "Firma", message.company))
    if message.contact_name:
        link = _search_link(_LINKEDIN_PEOPLE, message.contact_name, message.contact_name)
        lines.append(f"👤 <b>Ansprechpartner:</b> {link}")
    if message.is_endcustomer is not None:
        who = "Endkunde" if message.is_endcustomer else "Vermittler"
        add(_labeled("🤝", "Auftraggeber", who))
    add(_labeled("📍", "Einsatzort", message.location))
    add(_labeled("🏠", "Remote", message.remote_label))
    add(_labeled("💼", "Beschäftigungsart", message.contract_type))
    add(_labeled("📊", "Auslastung", message.workload_label))
    add(_labeled("⏳", "Dauer", message.duration_label))
    add(_labeled("📅", "Start", message.start))
    add(_labeled("🕒", "Eingestellt", message.posted_ago))
    add(_labeled("✍️", "Bewerbung bis", message.expires_label))
    add(_labeled("🏭", "Branche", message.industry))
    add(_labeled("🗣", "Sprache", message.language))
    add(_labeled_list("🛠", "Skills", message.skills, limit=12))
    add(_labeled_list("✅", "Passt", message.reasons, limit=3))
    add(_labeled_list("🎯", "Deine Skills", message.matching_skills, limit=8))
    add(_labeled_list("⚠️", "Lücken", message.missing_requirements, limit=4))
    add(_labeled_list("🚩", "Risiken", message.risk_flags, limit=3))

    if message.description:
        text = message.description
        if len(text) > _DESCRIPTION_LIMIT:
            text = text[:_DESCRIPTION_LIMIT].rstrip() + " …"
        lines.append(f"\n📄 <b>Beschreibung:</b>\n{_esc(text)}")

    lines.append("")
    lines.append(f"🔗 {_link(message.url, 'Zum Projekt')}")
    if message.company:
        lines.append(f"🔎 {_search_link(_GOOGLE, message.company, 'Firma googeln')}")

    return "\n".join(lines)


def apply_keyboard(listing_id: int) -> dict[str, object]:
    """Inline keyboard under a match message: one Apply button."""
    return {"inline_keyboard": [[{"text": "📝 Bewerben", "callback_data": f"apply:{listing_id}"}]]}


def draft_keyboard(application_id: int, *, can_send: bool) -> dict[str, object]:
    """Inline keyboard under a draft: Send (only with a recipient) and Discard."""
    row: list[dict[str, object]] = []
    if can_send:
        row.append({"text": "📤 Senden", "callback_data": f"send:{application_id}"})
    row.append({"text": "❌ Verwerfen", "callback_data": f"cancel:{application_id}"})
    return {"inline_keyboard": [row]}


def format_draft(view: DraftView) -> str:
    """Render the draft summary: recipient, subject, mail-client link, LinkedIn.

    The full e-mail body is delivered separately via ``email_body_messages`` so it
    is shown in full and never truncated.
    """
    lines = [f"📨 <b>Bewerbungsentwurf:</b> {_esc(view.title)}"]
    if view.url:
        lines.append(f"🔗 {_link(view.url, 'Zum Projekt')}")
    lines.append("")
    lines.append(f"📧 <b>An:</b> {_esc(view.recipient) if view.recipient else '❓ unbekannt'}")
    lines.append(f"✉️ <b>Betreff (zum Kopieren):</b> <code>{_esc(view.subject)}</code>")
    if view.recipient:
        mailto = f"mailto:{view.recipient}?subject={quote(view.subject)}"
        lines.append(f"📧 {_link(mailto, 'Im Mail-Client öffnen (Empfänger + Betreff)')}")
    lines.append("")
    lines.append(
        f"💬 <b>LinkedIn ({len(view.linkedin_message)}/{LINKEDIN_LIMIT}) - zum Kopieren:</b>"
    )
    lines.append(f"<pre>{_esc(view.linkedin_message)}</pre>")
    lines.append("")
    if view.status is ApplicationStatus.AWAITING_EMAIL:
        lines.append(
            "❗ Keine Empfänger-Adresse gefunden - antworte auf diese Nachricht "
            "mit der E-Mail-Adresse."
        )
    if view.revision_count:
        lines.append(f"🔁 Überarbeitung #{view.revision_count}")
    lines.append(
        "✏️ Antworte auf diese Nachricht, um Änderungen zu beschreiben"
        + (" - oder tippe Senden." if view.recipient else ".")
    )
    return "\n".join(lines)


def _split_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into ``<= limit`` chunks, preferring line boundaries.

    A single line longer than ``limit`` is hard-split; nothing is dropped.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def email_body_messages(view: DraftView) -> list[str]:
    """The full e-mail body as one or more copyable ``<pre>`` messages.

    Long applications are split across several messages (Telegram caps a message
    at 4096 chars) so the whole e-mail is always visible and copyable, never cut.
    """
    body = view.body.strip() or "(leer)"
    parts = _split_text(body, _TELEGRAM_TEXT_LIMIT)
    total = len(parts)
    messages: list[str] = []
    for index, part in enumerate(parts):
        header = "📄 <b>Vollständige E-Mail (zum Kopieren):</b>\n" if index == 0 else ""
        footer = f"\n<i>Teil {index + 1}/{total}</i>" if total > 1 else ""
        messages.append(f"{header}<pre>{_esc(part)}</pre>{footer}")
    return messages


class TelegramClient:
    """Minimal Bot API client: sendMessage with HTML, returning success as a bool."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(
        self,
        text: str,
        *,
        disable_preview: bool = True,
        reply_markup: dict[str, object] | None = None,
    ) -> int | None:
        """Send an HTML message; return its message id, or None on any failure."""
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        body = await self._call("sendMessage", payload)
        if body is None:
            return None
        result = body.get("result")
        if isinstance(result, dict):
            message_id = result.get("message_id")
            if isinstance(message_id, int):
                return message_id
        return None

    async def send_message(self, text: str, *, disable_preview: bool = True) -> bool:
        """Send an HTML message; return True only on a Bot API ``ok`` response."""
        return await self.send(text, disable_preview=disable_preview) is not None

    async def send_match(self, text: str, *, listing_id: int) -> bool:
        """Send a match message with its Apply button."""
        return await self.send(text, reply_markup=apply_keyboard(listing_id)) is not None

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> bool:
        """Acknowledge a button tap (stops the client-side spinner)."""
        payload: dict[str, object] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        return await self._call("answerCallbackQuery", payload) is not None

    async def get_updates(
        self, *, offset: int | None = None, timeout_s: int = 25
    ) -> list[TelegramUpdate] | None:
        """Long-poll for updates; None signals a failed call (caller backs off)."""
        payload: dict[str, object] = {
            "timeout": timeout_s,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        body = await self._call("getUpdates", payload, request_timeout=timeout_s + 10)
        if body is None:
            return None
        result = body.get("result")
        if not isinstance(result, list):
            return None
        updates: list[TelegramUpdate] = []
        for item in result:
            try:
                updates.append(TelegramUpdate.model_validate(item))
            except ValueError:  # unexpected shapes must never kill the poll loop
                logger.warning("skipping unparsable telegram update: %r", item)
                update_id = item.get("update_id") if isinstance(item, dict) else None
                if isinstance(update_id, int):  # still advance the offset past it
                    updates.append(TelegramUpdate(update_id=update_id))
        return updates

    async def _call(
        self, method: str, payload: dict[str, object], *, request_timeout: float | None = None
    ) -> dict[str, object] | None:
        """POST one Bot API method; return the response body only on ``ok``."""
        try:
            response = await self._client.post(
                f"{self._base_url}/{method}",
                json=payload,
                timeout=(
                    request_timeout if request_timeout is not None else httpx.USE_CLIENT_DEFAULT
                ),
            )
        except httpx.HTTPError as err:
            logger.warning("telegram %s failed (transport): %s", method, err)
            return None
        if response.status_code != 200:
            logger.warning("telegram %s failed (status %s)", method, response.status_code)
            return None
        try:
            body: object = response.json()
        except ValueError:
            logger.warning("telegram %s returned a non-JSON body", method)
            return None
        if not isinstance(body, dict) or not body.get("ok"):
            detail = body.get("description") if isinstance(body, dict) else body
            logger.warning("telegram %s rejected: %s", method, detail)
            return None
        return body

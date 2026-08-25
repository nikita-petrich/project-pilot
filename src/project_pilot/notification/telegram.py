"""Telegram push: THE notification channel.

Every match is sent from the worker itself, seconds after the verdict, over one
HTTP POST with retry. That is the whole point of this module: delivery must not
depend on a model deciding a run is "worth telling you about", which is how the
previous channel (a Claude routine whose completion push was a per-run model
decision) lost notifications.

Send-only, deliberately. There is no polling loop, no webhook and no inbound
port: the message carries an inline button to the Claude project that holds the
match chats (``CLAUDE_PROJECT_URL``), and every action — checking, drafting,
sending — happens there through the skills and the MCP tools. Nothing to
maintain here but one endpoint.

A match arrives as one card carrying the whole listing — the facts, the verdict
and the description — under three buttons: open it on its board, accept it, or
decline it. Accepting drafts the application and points at the Claude project;
declining deletes the card. Both are handled by
:mod:`~project_pilot.telegram_bot`; the callback data carries the listing id, so
a press is never resolved against some "current" listing.

Telegram was chosen over a push service for one reason the alternatives could
not match: its desktop app delivers a real system notification with nothing
open, where browser-based push needs a running browser and lapses after a week
of inactivity.
"""

import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from project_pilot.notification.messages import MatchMessage, headline, render_match_card

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
# Telegram rejects anything past 4096 characters outright.
MAX_TEXT_CHARS = 4_000
# What is left for the listing text once the card and the labels are in.
MAX_DESCRIPTION_CHARS = 2_000
_TIMEOUT = 15.0


def _is_retryable(err: BaseException) -> bool:
    """Network trouble and 5xx/429 retry; any other 4xx is a config error."""
    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        return status >= 500 or status == 429
    return isinstance(err, httpx.TransportError)


def match_text(message: MatchMessage) -> str:
    """The whole listing in one message: headline, card, facts, description.

    The card decides at a glance; the description is what the decision is
    actually made on, so it travels with it rather than living one tap away.
    """
    parts = [headline(message), render_match_card(message)]

    details = [
        ("Vertragsart", message.contract_type),
        ("Branche", message.industry),
        ("Sprache", message.language),
        ("Anzeige läuft", message.expires_label),
        ("Ansprechpartner", message.contact_name),
    ]
    extra = [f"{label}: {value}" for label, value in details if value]
    if message.skills:
        extra.append("Skills: " + ", ".join(message.skills))
    if message.missing_requirements:
        extra.append("Lücken: " + ", ".join(message.missing_requirements))
    if extra:
        parts.append("\n".join(extra))

    if message.description:
        text = message.description.strip()
        if len(text) > MAX_DESCRIPTION_CHARS:
            text = text[:MAX_DESCRIPTION_CHARS].rstrip() + " …"
        parts.append(f"Beschreibung:\n{text}")
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


def match_keyboard(message: MatchMessage) -> dict[str, object] | None:
    """The three buttons under a match card.

    The listing id rides in every callback (``accept:42``) rather than being
    resolved against a "current" listing, so two cards can never be confused.
    Without a stored listing there is nothing to accept or decline — a manual
    check has no id — and only the link remains.
    """
    rows: list[list[dict[str, str]]] = []
    if message.url:
        rows.append([{"text": "🔗 Projekt öffnen", "url": message.url}])
    if message.listing_id is not None:
        rows.append(
            [
                {"text": "✅ Annehmen", "callback_data": f"accept:{message.listing_id}"},
                {"text": "🗑 Abnehmen", "callback_data": f"decline:{message.listing_id}"},
            ]
        )
    return {"inline_keyboard": rows} if rows else None


class TelegramNotifier:
    """Sends one match (or one warning) to a Telegram chat."""

    def __init__(self, *, bot_token: str, chat_id: str) -> None:
        self._api = f"{API_BASE}/bot{bot_token}"
        self._chat_id = chat_id

    async def notify(self, message: MatchMessage) -> bool:
        """Send one match card with its buttons; False when delivery failed.

        A failed send must not fail the pipeline run: the listing stays
        unnotified and is retried on the next run.
        """
        payload: dict[str, object] = {"text": match_text(message)}
        keyboard = match_keyboard(message)
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            await self._post("sendMessage", payload)
        except httpx.HTTPError as err:
            logger.warning("telegram send failed for %s: %s", message.url, err)
            return False
        return True

    async def notify_warning(self, text: str) -> bool:
        """Deliver an operator warning; False on failure.

        Warnings are best-effort by design (the callers already log them), so
        this never raises either.
        """
        try:
            await self._post(
                "sendMessage",
                {"text": f"⚠️ project-pilot Betriebswarnung\n\n{text}"[:MAX_TEXT_CHARS]},
            )
        except httpx.HTTPError as err:
            logger.warning("telegram warning send failed: %s", err)
            return False
        return True

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1.0, max=10.0),
        reraise=True,
    )
    async def _post(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{self._api}/{method}",
                json={
                    "chat_id": self._chat_id,
                    # No parse_mode: the card is plain text with emoji, and any
                    # markup mode would make a stray character in a listing
                    # title (an underscore, an asterisk) reject the whole
                    # message.
                    "disable_web_page_preview": True,
                    **payload,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

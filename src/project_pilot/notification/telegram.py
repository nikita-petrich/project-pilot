"""Telegram push: THE notification channel.

Every match is sent from the worker itself, seconds after the verdict, over one
HTTP POST with retry. That is the whole point of this module: delivery must not
depend on a model deciding a run is "worth telling you about", which is how the
previous channel (a Claude routine whose completion push was a per-run model
decision) lost notifications.

Send-only, deliberately. There is no polling loop, no webhook and no inbound
port here: every action — checking, drafting, sending — happens in the post's
comment thread, where the thread agent answers (``telegram_bot.py``, its own
process).

The target is a **channel**. Telegram forwards each post into the channel's
linked discussion group by itself and roots a comment thread on the forwarded
copy, so one project is one post you can open into its own conversation — and
declining it is a plain ``deleteMessage`` on the post, which is what makes a
turned-down match vanish from the feed entirely.

The id of the sent post is what ``notify`` returns: it is the only handle that
ties the card to the comment thread the automatic forward is about to create,
and the bot needs it to route a reply back to its listing.

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

from project_pilot.notification.messages import MatchMessage, headline, render_match_details

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
# Telegram rejects anything past 4096 characters outright.
MAX_TEXT_CHARS = 4_000
_TIMEOUT = 15.0


def _is_retryable(err: BaseException) -> bool:
    """Network trouble and 5xx/429 retry; any other 4xx is a config error."""
    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        return status >= 500 or status == 429
    return isinstance(err, httpx.TransportError)


def match_text(message: MatchMessage) -> str:
    """The message body: the headline, then every fact and the verdict.

    The description is deliberately absent — it rides behind its own button, so
    a listing of several thousand characters cannot push the facts off the
    first screen.
    """
    return "\n\n".join([headline(message), render_match_details(message)])[:MAX_TEXT_CHARS]


def match_keyboard(message: MatchMessage) -> dict[str, object] | None:
    """The three decisions a match offers, or nothing for an unstored listing.

    The callbacks carry the listing id, so a press is unambiguous even when
    several matches are open at once.
    """
    if message.listing_id is None:
        return None
    listing_id = message.listing_id
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Annehmen", "callback_data": f"accept:{listing_id}"},
                {"text": "🚫 Ablehnen", "callback_data": f"decline:{listing_id}"},
            ],
            [{"text": "📄 Projektbeschreibung", "callback_data": f"describe:{listing_id}"}],
        ]
    }


class TelegramNotifier:
    """Sends one match (or one warning) to the Telegram channel."""

    def __init__(self, *, bot_token: str, chat_id: str) -> None:
        self._api = f"{API_BASE}/bot{bot_token}"
        self._chat_id = chat_id

    async def notify(self, message: MatchMessage) -> int | None:
        """Post one match to the channel; its message id, or None on failure.

        A failed send must not fail the pipeline run: the listing stays
        unnotified and is retried on the next run. The returned id is stored,
        because it is how the comment thread Telegram is about to open gets
        matched back to this listing.
        """
        payload: dict[str, object] = {"text": match_text(message)}
        keyboard = match_keyboard(message)
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            body = await self._post("sendMessage", payload)
        except httpx.HTTPError as err:
            logger.warning("telegram send failed for %s: %s", message.url, err)
            return None
        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int):
            logger.warning("telegram returned no message_id for %s", message.url)
            return None
        return message_id

    async def notify_warning(self, text: str) -> bool:
        """Deliver an operator warning; False on failure.

        Warnings are best-effort by design (the callers already log them), so
        this never raises either.
        """
        try:
            # A warning belongs to the worker, not to any one listing.
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

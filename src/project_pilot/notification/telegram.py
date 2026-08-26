"""Telegram push: THE notification channel.

Every match is sent from the worker itself, seconds after the verdict, over one
HTTP POST with retry. That is the whole point of this module: delivery must not
depend on a model deciding a run is "worth telling you about", which is how the
previous channel (a Claude routine whose completion push was a per-run model
decision) lost notifications.

Send-only, deliberately. There is no polling loop, no webhook and no inbound
port here: the message carries one inline button to the listing on its own
board, and every action — checking, drafting, sending — happens in the topic
itself, where the thread agent answers (``telegram_bot.py``, its own process).

Each match gets its own **forum topic** in the target supergroup, so one project
is one thread rather than one line in a growing chat. Telegram can close a topic,
which archives the whole exchange without deleting it, and its
``message_thread_id`` is the handle a later feature uses to route a reply back to
its listing.

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
# createForumTopic rejects a longer name rather than truncating it itself.
MAX_TOPIC_NAME_CHARS = 128
# icon_color takes six fixed values and nothing else. Blue marks a fresh match;
# the remaining colors become status in a later feature.
ICON_COLOR_BLUE = 7322096
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
    several topics are open at once.
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
    """Sends one match (or one warning) to a Telegram chat."""

    def __init__(self, *, bot_token: str, chat_id: str) -> None:
        self._api = f"{API_BASE}/bot{bot_token}"
        self._chat_id = chat_id

    async def create_topic(self, message: MatchMessage) -> int | None:
        """Open a forum topic for one match; None when the chat cannot host one.

        Returns None rather than raising for the same reason ``notify`` does: a
        chat that is not a forum, or a bot that is not an admin, must degrade to
        a plain message instead of failing the run.
        """
        name = headline(message)[:MAX_TOPIC_NAME_CHARS]
        try:
            payload = await self._post(
                "createForumTopic", {"name": name, "icon_color": ICON_COLOR_BLUE}
            )
        except httpx.HTTPError as err:
            logger.warning("telegram topic creation failed for %s: %s", message.url, err)
            return None
        result = payload.get("result")
        thread_id = result.get("message_thread_id") if isinstance(result, dict) else None
        if not isinstance(thread_id, int):
            logger.warning("telegram returned no message_thread_id for %s", message.url)
            return None
        return thread_id

    async def notify(self, message: MatchMessage, thread_id: int | None = None) -> bool:
        """Send one match, into its topic when it has one; False on failure.

        A failed send must not fail the pipeline run: the listing stays
        unnotified and is retried on the next run.
        """
        payload: dict[str, object] = {"text": match_text(message)}
        keyboard = match_keyboard(message)
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
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
            # No topic: a warning belongs to the worker, not to any one listing.
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

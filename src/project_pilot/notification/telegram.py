"""Telegram push: THE alert channel.

Every match is sent from the worker itself, seconds after the verdict, over one
HTTP POST with retry. That is the whole point of this module: delivery must not
depend on a model deciding a run is "worth telling you about", which is how a
Claude-side push lost notifications before — and the platform still offers no
guaranteed push for a cloud session, so the alert stays in code.

Outbound-only, deliberately. There is no polling loop, no webhook and no inbound
port here. The card is a decision surface and nothing more: two of its three
buttons are plain links — the original listing, and a new Claude chat with
the card already in its prompt (``claude_link.py``) — and only **Ablehnen**
needs a process to hear the press (``telegram_bot.py``, which does exactly that
and nothing else).

**Why Bewerben cannot be heard.** Telegram sends an update for a callback button
and none at all for a URL button, and a callback cannot open an arbitrary URL in
answer. A tap on Bewerben is therefore invisible to us, and making it audible
would cost the one tap that is the whole point of the card. So the card is not
removed on the tap but on its consequence: the chat that opens drafts the
application, and that draft takes the card off the feed over ``delete_card``
(:mod:`project_pilot.mcp_server`). A tap that leads nowhere leaves the card
standing, which is the right outcome.

The target is the private chat between Nik and the bot. A bot may delete its
own messages there, which is what makes a declined match vanish from the feed.

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

from project_pilot.notification.claude_link import NEW_CHAT_URL, session_link
from project_pilot.notification.messages import MatchMessage, headline, render_match_details

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
# Telegram rejects anything past 4096 characters outright.
MAX_TEXT_CHARS = 4_000
_TIMEOUT = 15.0

OPEN_LISTING = "📄 Projektbeschreibung öffnen"
APPLY = "✅ Bewerben"
DECLINE = "🚫 Ablehnen"
DECLINE_ACTION = "decline"


def _is_retryable(err: BaseException) -> bool:
    """Network trouble and 5xx/429 retry; any other 4xx is a config error."""
    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        return status >= 500 or status == 429
    return isinstance(err, httpx.TransportError)


def _is_link(url: str) -> bool:
    """Telegram only accepts http(s) in a URL button; an ingested listing may have none."""
    return url.startswith(("http://", "https://"))


def match_text(message: MatchMessage) -> str:
    """The message body: the headline, then every fact and the verdict.

    The description is deliberately absent — the listing link is one tap away,
    and a listing of several thousand characters would push the facts off the
    first screen.
    """
    return "\n\n".join([headline(message), render_match_details(message)])[:MAX_TEXT_CHARS]


def match_keyboard(
    message: MatchMessage, *, session_url: str = NEW_CHAT_URL
) -> dict[str, object] | None:
    """The three decisions a match offers.

    Bewerben and the listing are URL buttons: a tap opens a new Claude chat
    (this very card in its prompt) or the original ad, with no process in
    between. That costs the press itself — Telegram reports no URL tap — so the
    card leaves the feed when the chat drafts the application, not when the
    button is touched (see the module docstring). Ablehnen is the one callback;
    it carries the listing id so a press is unambiguous in the log, but the bot
    never looks the id up (deleting the card is the whole job), so an unstored
    listing (test-match) still gets a working Ablehnen with an empty id. A card
    without a real link (an ingested text) gets no link button.
    """
    listing_id = message.listing_id if message.listing_id is not None else ""
    top: list[dict[str, object]] = [
        {"text": APPLY, "url": session_link(message, base_url=session_url)},
        {"text": DECLINE, "callback_data": f"{DECLINE_ACTION}:{listing_id}"},
    ]
    rows = [top]
    if _is_link(message.url):
        rows.append([{"text": OPEN_LISTING, "url": message.url}])
    return {"inline_keyboard": rows}


class TelegramNotifier:
    """Sends one match (or one warning) to the Telegram chat."""

    def __init__(self, *, bot_token: str, chat_id: str, session_url: str = NEW_CHAT_URL) -> None:
        self._api = f"{API_BASE}/bot{bot_token}"
        self._chat_id = chat_id
        self._session_url = session_url

    async def notify(self, message: MatchMessage) -> int | None:
        """Send one match card; its message id, or None on failure.

        A failed send must not fail the pipeline run: the listing stays
        unnotified and is retried on the next run.
        """
        payload: dict[str, object] = {
            "text": match_text(message),
            "reply_markup": match_keyboard(message, session_url=self._session_url),
        }
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

    async def delete_card(self, message_id: int) -> bool:
        """Take one card off the feed; False if Telegram refused.

        Best-effort by design: a card that cannot be deleted (already gone, or
        older than Telegram's 48-hour window for bot deletions) is a cosmetic
        problem, never a reason to fail the work that triggered the removal.
        """
        try:
            await self._post("deleteMessage", {"message_id": message_id})
        except httpx.HTTPError as err:
            logger.info("telegram card %s not deleted: %s", message_id, err)
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

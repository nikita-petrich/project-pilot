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
_TIMEOUT = 15.0


def _is_retryable(err: BaseException) -> bool:
    """Network trouble and 5xx/429 retry; any other 4xx is a config error."""
    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        return status >= 500 or status == 429
    return isinstance(err, httpx.TransportError)


def match_text(message: MatchMessage) -> str:
    """The message body: the headline, the command to type, then the card.

    The command leads because the chat the button opens starts empty — the id is
    what gets typed there (``/check-project 42``), and the card below it is the
    overview that decides whether it is worth typing at all.
    """
    parts = [headline(message)]
    if message.listing_id is not None:
        parts.append(f"→ /check-project {message.listing_id}")
    parts.append(render_match_card(message))
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


class TelegramNotifier:
    """Sends one match (or one warning) to a Telegram chat."""

    def __init__(self, *, bot_token: str, chat_id: str, target_url: str = "") -> None:
        self._url = f"{API_BASE}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._target_url = target_url.strip()

    async def notify(self, message: MatchMessage) -> bool:
        """Send one match; False when delivery failed.

        A failed send must not fail the pipeline run: the listing stays
        unnotified and is retried on the next run.
        """
        # Never a dead end: without a configured project the button opens the
        # listing on its own board instead.
        link = self._target_url or message.url
        payload: dict[str, object] = {"text": match_text(message)}
        if link:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "🚀 Im Claude-Projekt öffnen", "url": link}]]
            }
        try:
            await self._post(payload)
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
                {"text": f"⚠️ project-pilot Betriebswarnung\n\n{text}"[:MAX_TEXT_CHARS]}
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
    async def _post(self, payload: dict[str, object]) -> None:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                self._url,
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

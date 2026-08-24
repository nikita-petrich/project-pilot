"""ntfy push: THE notification channel.

Every match is pushed from the worker itself, seconds after the verdict, over
one HTTP POST with retry. That is the whole point of this module: delivery must
not depend on a model deciding a run is "worth telling you about", which is how
the previous channel (a Claude routine whose completion push was a per-run model
decision) lost notifications.

The push carries the match card as its body, and its click target is the Claude
project that holds the match chats (``CLAUDE_PROJECT_URL``): one tap lands in a
place where the account skills and the project-pilot MCP tools are ready, and the
listing id from the card names what to work on. Falls back to the listing's own
URL when no project is configured, so a push is never a dead end. Operator
warnings travel the same channel at max priority.

Published as JSON rather than through ntfy's ``X-Title``/``X-Click`` headers:
titles carry emoji and German umlauts, and HTTP headers are not a safe place for
either.
"""

import logging
from urllib.parse import urlsplit

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from project_pilot.errors import ConfigError
from project_pilot.notification.messages import MatchMessage, headline, render_match_card

logger = logging.getLogger(__name__)

# ntfy's default per-message limit is 4 KiB; stay clear of it.
MAX_BODY_CHARS = 3_500
PRIORITY_MATCH = 4  # high: bypasses "silent" grouping on both mobile clients
PRIORITY_WARNING = 5  # max: an operator warning means the scan itself is degraded
_TIMEOUT = 15.0


def _is_retryable(err: BaseException) -> bool:
    """Network trouble and 5xx/429 retry; any other 4xx is a config error."""
    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        return status >= 500 or status == 429
    return isinstance(err, httpx.TransportError)


def split_topic_url(topic_url: str) -> tuple[str, str]:
    """Split ``https://ntfy.sh/my-topic`` into its server and topic.

    One setting rather than two, because the pair is copied out of the ntfy app
    as a single address.
    """
    parts = urlsplit(topic_url.strip().rstrip("/"))
    topic = parts.path.lstrip("/")
    if parts.scheme not in {"http", "https"} or not parts.netloc or not topic or "/" in topic:
        raise ConfigError(
            f"NTFY_TOPIC_URL is {topic_url!r}; expected https://<server>/<topic>, "
            "e.g. https://ntfy.sh/project-pilot-a8f3k2m9x"
        )
    return f"{parts.scheme}://{parts.netloc}", topic


def push_body(message: MatchMessage) -> str:
    """The notification body: the match card, plus the listing id to act on.

    The id leads, because it is what gets typed into the chat that the push
    opens (``/check-project 42``); the card below it is the overview.
    """
    parts = []
    if message.listing_id is not None:
        parts.append(f"→ /check-project {message.listing_id}")
    parts.append(render_match_card(message))
    return "\n\n".join(parts)[:MAX_BODY_CHARS]


class NtfyPush:
    """Pushes one match (or one warning) to an ntfy topic."""

    def __init__(self, *, topic_url: str, token: str = "", target_url: str = "") -> None:
        self._server, self._topic = split_topic_url(topic_url)
        self._target_url = target_url.strip()
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def notify(self, message: MatchMessage) -> bool:
        """Push one match; False when delivery failed.

        A failed push must not fail the pipeline run: the listing stays
        unnotified and is retried on the next run.
        """
        link = self._target_url or message.url
        try:
            await self._post(
                {
                    "title": headline(message),
                    "message": push_body(message),
                    "click": link,
                    "priority": PRIORITY_MATCH,
                }
            )
        except httpx.HTTPError as err:
            logger.warning("ntfy push failed for %s: %s", message.url, err)
            return False
        return True

    async def notify_warning(self, text: str) -> bool:
        """Deliver an operator warning; False on failure.

        Warnings are best-effort by design (the callers already log them), so
        this never raises either.
        """
        try:
            await self._post(
                {
                    "title": "⚠️ project-pilot Betriebswarnung",
                    "message": text[:MAX_BODY_CHARS],
                    "priority": PRIORITY_WARNING,
                }
            )
        except httpx.HTTPError as err:
            logger.warning("ntfy warning push failed: %s", err)
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
                self._server, headers=self._headers, json={"topic": self._topic, **payload}
            )
            response.raise_for_status()

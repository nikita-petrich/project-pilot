"""Open the Claude session a match is handled in: one routine fire per match.

A match POSTs the listing's facts to the ``match-thread`` routine's fire
endpoint; Claude creates one cloud session per call in Nik's own account and
returns its URL. That URL is what the Telegram card's **Bewerben** button opens,
so the session is where the match is worked — with the account skills and the
``project_pilot_*`` MCP tools — while delivery of the alert itself stays with
the worker (``telegram.py``), because the platform offers no guaranteed push
for a session created this way.

The fire endpoint is experimental (beta header below) and has no idempotency
key: every POST creates a new session. The caller guards against double fires
by only firing for listings without a stored session URL; this module stays
deliberately tiny so an API change stays a one-file fix.
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

# Required on every request; without it the endpoint returns 400.
BETA_HEADER = "experimental-cc-routine-2026-04-01"
# The text field is capped server-side at 65,536; leave generous headroom.
MAX_TEXT_CHARS = 60_000
_TIMEOUT = 30.0


def _is_retryable(err: BaseException) -> bool:
    """Network trouble and 5xx/429 retry; any other 4xx is a config error."""
    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        return status >= 500 or status == 429
    return isinstance(err, httpx.TransportError)


def fire_text(message: MatchMessage) -> str:
    """The session's opening context: headline, listing id, every fact, the text.

    The headline comes first because it is what a surface derives the session's
    name from; the listing id follows because it is the key to every MCP tool.
    The card is rendered here rather than left to the model, so the session
    opens on the same overview the Telegram alert showed.
    """
    parts = [headline(message)]
    if message.listing_id is not None:
        parts.append(f"Listing-ID: {message.listing_id}")
    parts.append(render_match_details(message))
    if message.description:
        parts.append(f"Beschreibung:\n{message.description}")
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


class ClaudeRoutineFire:
    """POSTs one match to the routine's fire endpoint and returns the session URL."""

    def __init__(self, *, fire_url: str, token: str) -> None:
        self._fire_url = fire_url
        self._headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": BETA_HEADER,
        }

    async def open_session(self, message: MatchMessage) -> str | None:
        """The new session's URL, or None when firing failed (never raises).

        A failed fire must not fail the pipeline run, and it must not hold the
        alert back either: the caller sends the card without a session and says
        so, because a match nobody hears about is the one failure this project
        exists to prevent.
        """
        try:
            payload = await self._post(fire_text(message))
        except (httpx.HTTPError, ValueError) as err:
            logger.warning("routine fire failed for %s: %s", message.url, err)
            return None
        session_url = payload.get("claude_code_session_url")
        if not isinstance(session_url, str) or not session_url:
            logger.warning("routine fire returned no session url for %s", message.url)
            return None
        return session_url

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1.0, max=10.0),
        reraise=True,
    )
    async def _post(self, text: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(self._fire_url, headers=self._headers, json={"text": text})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError(f"unexpected fire response: {data!r}")
            return data

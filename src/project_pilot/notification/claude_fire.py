"""Fire the Claude Code routine: THE notification channel.

A match POSTs the listing's facts to the ``match-thread`` routine's fire
endpoint; Claude creates one session per match, summarizes it, and the Claude
app pushes the completion to the phone — the whole notification surface of the
target architecture (``blueprint/reference/zielarchitektur.drawio``). Operator
warnings (cooldown, LLM health, consecutive failures) travel the same way as
plain-text sessions.

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

from project_pilot.notification.messages import MatchMessage, headline, render_match_card

logger = logging.getLogger(__name__)

# Required on every request; without it the endpoint returns 400.
BETA_HEADER = "experimental-cc-routine-2026-04-01"
# The text field is capped server-side; leave generous headroom under 65,536.
MAX_TEXT_CHARS = 60_000
_TIMEOUT = 30.0


def _is_retryable(err: BaseException) -> bool:
    """Network trouble and 5xx/429 retry; any other 4xx is a config error."""
    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        return status >= 500 or status == 429
    return isinstance(err, httpx.TransportError)


def fire_text(message: MatchMessage) -> str:
    """The session's opening context: the match card, the rest of the facts, the text.

    The card comes first and is rendered here rather than left to the model, so
    the overview that reaches the feed and the push looks the same every time.
    The routine prompt repeats it verbatim and adds its own reading below it.
    """
    # The opening line is what surfaces as the session's name in the feed, so it
    # leads with the three things that identify a match at a glance.
    parts = [headline(message)]
    if message.listing_id is not None:
        parts.append(f"Listing-ID: {message.listing_id}")
    parts.append(render_match_card(message))

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
    if message.matching_skills:
        extra.append("Passende Skills: " + ", ".join(message.matching_skills))
    if message.missing_requirements:
        extra.append("Lücken: " + ", ".join(message.missing_requirements))
    # The card shows the top two of each; the session gets the full lists.
    if len(message.reasons) > 1:
        extra.append("Warum Match: " + " · ".join(message.reasons))
    if len(message.risk_flags) > 1:
        extra.append("Risiken: " + " · ".join(message.risk_flags))
    if extra:
        parts.append("\n".join(extra))

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

    async def fire(self, message: MatchMessage) -> str | None:
        """The new session's URL, or None when firing failed (never raises).

        A failed fire must not fail the pipeline run: the listing stays
        unnotified and is retried on the next run, exactly like a failed send
        on the previous channel.
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

    async def fire_warning(self, text: str) -> bool:
        """Deliver an operator warning as its own session; False on failure.

        Warnings are best-effort by design (the callers already log them), so
        this never raises either.
        """
        try:
            await self._post(f"⚠️ project-pilot Betriebswarnung\n\n{text}"[:MAX_TEXT_CHARS])
        except (httpx.HTTPError, ValueError) as err:
            logger.warning("warning fire failed: %s", err)
            return False
        return True

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

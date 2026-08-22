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

from project_pilot.notification.messages import MatchMessage

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
    """The session's opening context: the card's facts plus the full description."""
    facts = [
        f"NEUES MATCH — Score {message.score}/100",
        f"Rolle: {message.title}",
    ]
    # The id is what lets the session reach the MCP tools for this listing, so it
    # goes in first — without it the thread can only work from the text below.
    if message.listing_id is not None:
        facts.insert(1, f"Listing-ID: {message.listing_id}")
    labeled = [
        ("Firma", message.company),
        ("Ort", message.location),
        ("Remote", message.remote_label),
        ("Start", message.start),
        ("Dauer", message.duration_label),
        ("Auslastung", message.workload_label),
        ("Vertragsart", message.contract_type),
    ]
    facts.extend(f"{label}: {value}" for label, value in labeled if value)
    if message.skills:
        facts.append("Skills: " + ", ".join(message.skills))
    if message.reasons:
        facts.append("Warum Match: " + " · ".join(message.reasons))
    if message.risk_flags:
        facts.append("Risiken: " + " · ".join(message.risk_flags))
    facts.append(f"URL: {message.url}")
    if message.description:
        facts.append(f"\nBeschreibung:\n{message.description}")
    return "\n".join(facts)[:MAX_TEXT_CHARS]


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

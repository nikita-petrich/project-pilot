"""Operator health signals: classify a dependency failure, then alert once per problem.

A rejected API key, an exhausted account or a wrong ``LLM_MODEL`` never crashes the
worker. Scraping keeps filling the database, every listing is stored with the
``llm_error`` fallback verdict, the run is recorded as a success — and the channel
simply stays quiet. That is the failure mode nobody notices, so it is turned into an
explicit message here: what is broken, which setting fixes it, and what it currently
costs. Repeats are throttled so a long outage stays one message plus reminders, and
recovery is announced so silence again means "nothing to do".
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from openai import APIConnectionError

logger = logging.getLogger(__name__)

LLM_COMPONENT = "llm"

# A still-broken dependency is re-announced only this much later: long enough that an
# evening outage is one message rather than a wall of them, short enough to remain a
# standing reminder that the pilot is not doing its job.
DEFAULT_REPEAT_AFTER = timedelta(hours=6)
# Alerts are read on a phone; the provider's own error is a diagnostic tail, not
# the message, so it is trimmed to one glance.
MAX_DETAIL_CHARS = 300

LLM_IMPACT = (
    "Listings are still collected and stored, but every one of them is scored "
    "`llm_error`, so no match alert can fire until this is fixed."
)


class HealthKind(StrEnum):
    """Why a dependency is failing — one class per thing the operator would do about it."""

    MODEL_NOT_FOUND = "model_not_found"
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    UPSTREAM = "upstream"
    SCHEMA = "schema"
    UNKNOWN = "unknown"


# Retrying a wrong model name, a rejected key or an empty account is pure waste: the
# identical call fails identically. Same rule as the scraper's "never retry a 403".
_RETRYABLE = frozenset(
    {
        HealthKind.RATE_LIMIT,
        HealthKind.CONNECTION,
        HealthKind.UPSTREAM,
        HealthKind.SCHEMA,
        HealthKind.UNKNOWN,
    }
)

_QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"})
_MODEL_CODES = frozenset({"model_not_found", "model_not_available"})

_LLM_SUMMARIES: dict[HealthKind, str] = {
    HealthKind.MODEL_NOT_FOUND: (
        "the LLM model '{model}' does not exist or this API key cannot use it "
        "(HTTP 404) — fix `LLM_MODEL`"
    ),
    HealthKind.AUTH: "OpenAI rejected the API key — check `OPENAI_API_KEY`",
    HealthKind.QUOTA: ("the OpenAI account is out of credit or over its billing limit — top it up"),
    HealthKind.RATE_LIMIT: "OpenAI is rate limiting project-pilot",
    HealthKind.CONNECTION: "the OpenAI API is unreachable from the server",
    HealthKind.UPSTREAM: "OpenAI returned a server error",
    HealthKind.SCHEMA: "the model '{model}' returned no usable verdict",
    HealthKind.UNKNOWN: "the LLM call to '{model}' failed",
}


@dataclass(frozen=True, slots=True)
class HealthIssue:
    """One dependency being unhealthy, phrased for the person who has to fix it."""

    component: str
    kind: HealthKind
    summary: str
    impact: str = ""
    detail: str = ""

    @property
    def is_retryable(self) -> bool:
        """Whether repeating the identical call could plausibly succeed."""
        return self.kind in _RETRYABLE

    @property
    def fingerprint(self) -> str:
        """Identity of the *problem*, so a different cause alerts instead of being throttled."""
        return f"{self.component}:{self.kind.value}"

    def as_message(self) -> str:
        """The operator-facing alert: what broke, what it costs, what the provider said."""
        lines = [f"⚠️ project-pilot: {self.summary}."]
        if self.impact:
            lines.append(self.impact)
        if self.detail:
            lines.append(f"`{self.detail}`")
        return "\n".join(lines)


def _trim(detail: str) -> str:
    """One-line, backtick-free diagnostic tail (kept safe for message formatting)."""
    flat = " ".join(detail.split()).replace("`", "'")
    if len(flat) <= MAX_DETAIL_CHARS:
        return flat
    return flat[: MAX_DETAIL_CHARS - 1] + "…"


def _status_code(err: BaseException) -> int | None:
    value = getattr(err, "status_code", None)
    return value if isinstance(value, int) else None


def _error_code(err: BaseException) -> str | None:
    value = getattr(err, "code", None)
    return value if isinstance(value, str) else None


def _llm_kind(err: BaseException) -> HealthKind:
    """Classify by the provider's own status/code, so any client implementation fits."""
    status = _status_code(err)
    code = _error_code(err)
    if code in _QUOTA_CODES:
        return HealthKind.QUOTA
    if code in _MODEL_CODES or status == 404:
        return HealthKind.MODEL_NOT_FOUND
    if status in (401, 403):
        return HealthKind.AUTH
    if status == 429:
        return HealthKind.RATE_LIMIT
    if status is not None and status >= 500:
        return HealthKind.UPSTREAM
    if isinstance(err, APIConnectionError | OSError):
        return HealthKind.CONNECTION
    return HealthKind.UNKNOWN


def llm_issue(
    kind: HealthKind, *, model: str, detail: str = "", component: str = LLM_COMPONENT
) -> HealthIssue:
    """Build the operator-facing issue for a known LLM failure class."""
    return HealthIssue(
        component=component,
        kind=kind,
        summary=_LLM_SUMMARIES[kind].format(model=model),
        impact=LLM_IMPACT,
        detail=_trim(detail),
    )


def classify_llm_error(
    err: BaseException, *, model: str, component: str = LLM_COMPONENT
) -> HealthIssue:
    """Map a failed LLM call onto the setting or account that has to change."""
    return llm_issue(
        _llm_kind(err), model=model, detail=f"{type(err).__name__}: {err}", component=component
    )


type SendAlert = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Active:
    fingerprint: str
    alerted_at: datetime


class HealthAlerter:
    """Turns a repeating health issue into few, useful messages.

    One message when a problem appears, a reminder only once it has survived
    ``repeat_after``, and one message when the component answers again. A *different*
    cause for the same component (model name fixed, account now empty) alerts straight
    away instead of hiding behind the reminder window.

    The state is per process, which is what the ``daemon`` needs; a cron-driven
    ``run-once`` starts fresh and therefore alerts on every broken run — noisier, but
    never silent, which is the property that matters here.
    """

    def __init__(self, send: SendAlert, *, repeat_after: timedelta = DEFAULT_REPEAT_AFTER) -> None:
        self._send = send
        self._repeat_after = repeat_after
        self._active: dict[str, _Active] = {}

    async def failed(self, issue: HealthIssue, *, now: datetime) -> None:
        """Report a component as unhealthy, sending at most one message per problem."""
        active = self._active.get(issue.component)
        if (
            active is not None
            and active.fingerprint == issue.fingerprint
            and now - active.alerted_at < self._repeat_after
        ):
            logger.warning("%s still unhealthy: %s", issue.component, issue.summary)
            return
        self._active[issue.component] = _Active(issue.fingerprint, now)
        logger.error("%s unhealthy: %s (%s)", issue.component, issue.summary, issue.detail)
        await self._send(issue.as_message())

    async def recovered(self, component: str) -> None:
        """Announce recovery, but only for a component that was actually reported broken."""
        if self._active.pop(component, None) is None:
            return
        logger.info("%s is healthy again", component)
        await self._send(f"✅ project-pilot: the {component} is answering again.")

"""Manual match check (Slack ``/check``): hard rules + LLM verdict for one input.

Runs the scan pipeline's stages 2-3 for a single manually supplied listing (URL,
pasted text, or an uploaded file's text). Deliberately read-only: nothing is
persisted, the freshness gate is skipped (a manual check is always wanted), and
the scan watermark stays untouched.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.db import session_scope
from project_pilot.errors import ApplicationStateError
from project_pilot.evaluation.llm import (
    LlmEvaluation,
    is_match_notifiable,
    render_listing,
    render_listing_entity,
)
from project_pilot.evaluation.rules import apply_hard_rules
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import EvaluationStage, Listing, Verdict
from project_pilot.notification.messages import MatchMessage, to_match_message
from project_pilot.profile_loader import Profile
from project_pilot.repository import Repository


class Matcher(Protocol):
    async def evaluate(self, *, profile_text: str, listing_text: str) -> LlmEvaluation: ...


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of one manual check, ready for rendering.

    ``passed`` means "would have been notified": an LLM match verdict with a
    score at or above the configured threshold. ``message`` is only built for a
    pass, so the bot can post the same message a real scan match produces.
    """

    title: str
    stage: EvaluationStage
    verdict: Verdict
    passed: bool
    score: int | None
    threshold: int
    reason: dict[str, object]
    message: MatchMessage | None
    is_llm_error: bool


class CheckService:
    """Evaluates one listing (stored, freshly parsed, or raw text) like the scan would."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        matcher: Matcher,
        profile: Profile,
        threshold: int,
    ) -> None:
        self._session_factory = session_factory
        self._matcher = matcher
        self._profile = profile
        self._threshold = threshold

    async def check_stored(self, listing_id: int) -> CheckResult:
        """Check a listing already in the database (``/check`` with a known URL)."""
        async with session_scope(self._session_factory) as session:
            listing = await Repository(session).get_listing(listing_id)
            if listing is None:
                raise ApplicationStateError(f"Project {listing_id} not found")
            return await self._evaluate(
                title=listing.title,
                rules_text=f"{listing.title}\n{listing.description}",
                listing_text=render_listing_entity(listing),
                match_source=listing,
            )

    async def check_parsed(self, parsed: ParsedListing) -> CheckResult:
        """Check a freshly fetched detail page (``/check`` with an unknown URL)."""
        return await self._evaluate(
            title=parsed.title,
            rules_text=f"{parsed.title}\n{parsed.description}",
            listing_text=render_listing(parsed),
            match_source=parsed,
        )

    async def check_text(self, text: str) -> CheckResult:
        """Check a pasted project description or an uploaded file's text."""
        stripped = text.strip()
        title = stripped.splitlines()[0][:120] if stripped else "Projekt"
        return await self._evaluate(
            title=title, rules_text=stripped, listing_text=stripped, match_source=None
        )

    async def _evaluate(
        self,
        *,
        title: str,
        rules_text: str,
        listing_text: str,
        match_source: Listing | ParsedListing | None,
    ) -> CheckResult:
        rule = apply_hard_rules(rules_text, self._profile.constraints)
        if not rule.passed:
            return CheckResult(
                title=title,
                stage=EvaluationStage.HARD_RULE,
                verdict=Verdict.NO_MATCH,
                passed=False,
                score=None,
                threshold=self._threshold,
                reason=rule.reason,
                message=None,
                is_llm_error=False,
            )
        llm = await self._matcher.evaluate(
            profile_text=self._profile.text, listing_text=listing_text
        )
        passed = is_match_notifiable(llm, self._threshold)
        return CheckResult(
            title=title,
            stage=EvaluationStage.LLM,
            verdict=Verdict.MATCH if llm.is_match else Verdict.NO_MATCH,
            passed=passed,
            score=llm.score,
            threshold=self._threshold,
            reason=llm.reason(),
            message=self._match_message(title, llm, match_source, listing_text) if passed else None,
            is_llm_error=llm.is_error,
        )

    def _match_message(
        self,
        title: str,
        llm: LlmEvaluation,
        match_source: Listing | ParsedListing | None,
        listing_text: str,
    ) -> MatchMessage:
        verdict = llm.verdict
        if match_source is None:  # raw text has no listing fields beyond the text itself
            return MatchMessage(
                title=title,
                url="",
                score=llm.score,
                reasons=list(verdict.reasons),
                matching_skills=list(verdict.matching_skills),
                missing_requirements=list(verdict.missing_requirements),
                risk_flags=list(verdict.risk_flags),
                description=listing_text,
            )
        return to_match_message(
            match_source,
            datetime.now(UTC),
            score=llm.score,
            reasons=list(verdict.reasons),
            matching_skills=list(verdict.matching_skills),
            missing_requirements=list(verdict.missing_requirements),
            risk_flags=list(verdict.risk_flags),
        )

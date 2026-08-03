"""Reporting queries: verdict distribution, matches over time, token costs."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from project_pilot.models import (
    Evaluation,
    EvaluationStage,
    Listing,
    Run,
    RunStatus,
    SourceState,
    Verdict,
)

HEALTH_INTERVAL_MULTIPLE = 3


@dataclass(frozen=True, slots=True)
class TokenUsage:
    llm_calls: int
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True, slots=True)
class Report:
    days: int
    total_listings: int
    listings_by_status: dict[str, int]
    verdicts: dict[str, int]
    matches_per_day: list[tuple[str, int]]
    top_no_match_terms: list[tuple[str, int]]
    tokens: TokenUsage


class ReportingService:
    """Read-only aggregate queries over listings and evaluations."""

    def __init__(self, session: AsyncSession, *, now: datetime | None = None) -> None:
        self._session = session
        self._now = now or datetime.now(UTC)

    async def total_listings(self) -> int:
        count = await self._session.scalar(select(func.count()).select_from(Listing))
        return int(count or 0)

    async def listings_by_status(self) -> dict[str, int]:
        rows = await self._session.execute(
            select(Listing.status, func.count()).group_by(Listing.status)
        )
        return {status.value: count for status, count in rows.all()}

    async def verdict_distribution(self) -> dict[str, int]:
        rows = await self._session.execute(
            select(Evaluation.verdict, func.count()).group_by(Evaluation.verdict)
        )
        return {verdict.value: count for verdict, count in rows.all()}

    async def matches_per_day(self, days: int) -> list[tuple[str, int]]:
        since = self._now - timedelta(days=days)
        day = func.date(Evaluation.created_at)
        rows = await self._session.execute(
            select(day, func.count())
            .where(
                Evaluation.stage == EvaluationStage.LLM,
                Evaluation.verdict == Verdict.MATCH,
                Evaluation.created_at >= since,
            )
            .group_by(day)
            .order_by(day)
        )
        return [(str(bucket), count) for bucket, count in rows.all()]

    async def top_no_match_terms(self, limit: int) -> list[tuple[str, int]]:
        term = func.jsonb_extract_path_text(Evaluation.reason, "matched_term")
        rows = await self._session.execute(
            select(term, func.count())
            .where(
                Evaluation.stage == EvaluationStage.HARD_RULE,
                Evaluation.verdict == Verdict.NO_MATCH,
                term.is_not(None),
            )
            .group_by(term)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(str(value), count) for value, count in rows.all()]

    async def token_usage(self, days: int) -> TokenUsage:
        since = self._now - timedelta(days=days)
        row = await self._session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(Evaluation.tokens_in), 0),
                func.coalesce(func.sum(Evaluation.tokens_out), 0),
            ).where(Evaluation.stage == EvaluationStage.LLM, Evaluation.created_at >= since)
        )
        calls, tokens_in, tokens_out = row.one()
        return TokenUsage(
            llm_calls=int(calls), tokens_in=int(tokens_in), tokens_out=int(tokens_out)
        )

    async def last_successful_run_at(self) -> datetime | None:
        stmt = (
            select(Run.finished_at)
            .where(
                Run.status.in_([RunStatus.SUCCESS, RunStatus.PARTIAL]),
                Run.finished_at.is_not(None),
            )
            .order_by(Run.finished_at.desc())
            .limit(1)
        )
        return await self._session.scalar(stmt)

    async def active_cooldown_until(self) -> datetime | None:
        """The latest active 403/captcha cooldown boundary, if any source is paused."""
        return await self._session.scalar(
            select(func.max(SourceState.cooldown_until)).where(
                SourceState.cooldown_until > self._now
            )
        )

    async def is_healthy(self, *, interval_minutes: int) -> bool:
        """True if the last successful run finished within 3x the scan interval.

        An active cooldown also counts as healthy: the worker is deliberately
        paused after a 403/captcha, not broken — without this, a by-design pause
        would flag the container unhealthy for hours.
        """
        if await self.active_cooldown_until() is not None:
            return True
        finished_at = await self.last_successful_run_at()
        if finished_at is None:
            return False
        max_age = timedelta(minutes=HEALTH_INTERVAL_MULTIPLE * interval_minutes)
        return self._now - finished_at <= max_age

    async def build_report(self, *, days: int = 7) -> Report:
        return Report(
            days=days,
            total_listings=await self.total_listings(),
            listings_by_status=await self.listings_by_status(),
            verdicts=await self.verdict_distribution(),
            matches_per_day=await self.matches_per_day(days),
            top_no_match_terms=await self.top_no_match_terms(10),
            tokens=await self.token_usage(days),
        )


def format_report(report: Report) -> str:
    """Render a report as a compact plain-text summary for the CLI."""
    lines = [
        "project-pilot stats",
        f"  listings total: {report.total_listings}",
        f"  by status: {_kv(report.listings_by_status)}",
        f"  verdicts: {_kv(report.verdicts)}",
        f"  matches (last {report.days}d): "
        + (", ".join(f"{day}={count}" for day, count in report.matches_per_day) or "none"),
        "  top no-match terms: "
        + (", ".join(f"{term}={count}" for term, count in report.top_no_match_terms) or "none"),
        f"  LLM (last {report.days}d): calls={report.tokens.llm_calls} "
        f"tokens_in={report.tokens.tokens_in} tokens_out={report.tokens.tokens_out}",
    ]
    return "\n".join(lines)


def _kv(mapping: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(mapping.items())) or "none"

"""Data-access layer: known-hash lookup, listing upsert, run and state persistence."""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from project_pilot.models import (
    Application,
    Evaluation,
    EvaluationStage,
    Listing,
    Run,
    RunStatus,
    SourceState,
    Verdict,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Repository:
    """Async data access bound to one session (one unit of work per run)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_listings(self) -> int:
        count = await self._session.scalar(select(func.count()).select_from(Listing))
        return int(count or 0)

    async def get_known_hashes(self, url_hashes: Iterable[str]) -> set[str]:
        hashes = list(url_hashes)
        if not hashes:
            return set()
        rows = await self._session.scalars(
            select(Listing.url_hash).where(Listing.url_hash.in_(hashes))
        )
        return set(rows.all())

    async def get_listing_by_hash(self, url_hash: str) -> Listing | None:
        result = await self._session.scalars(select(Listing).where(Listing.url_hash == url_hash))
        return result.first()

    async def upsert_listing(self, listing: Listing) -> tuple[Listing, bool]:
        """Insert a new listing, or touch ``last_seen_at`` on the known one (stage 0)."""
        existing = await self.get_listing_by_hash(listing.url_hash)
        if existing is not None:
            existing.last_seen_at = _utcnow()
            await self._session.flush()
            return existing, False
        self._session.add(listing)
        await self._session.flush()
        return listing, True

    async def add_evaluation(self, evaluation: Evaluation) -> Evaluation:
        self._session.add(evaluation)
        await self._session.flush()
        return evaluation

    async def start_run(self) -> Run:
        run = Run(started_at=_utcnow())
        self._session.add(run)
        await self._session.flush()
        return run

    async def finalize_run(
        self,
        run: Run,
        *,
        status: RunStatus,
        fetched: int = 0,
        new: int = 0,
        evaluated: int = 0,
        matched: int = 0,
        notified: int = 0,
        error: str | None = None,
    ) -> Run:
        run.status = status
        run.fetched = fetched
        run.new = new
        run.evaluated = evaluated
        run.matched = matched
        run.notified = notified
        run.error = error
        run.finished_at = _utcnow()
        await self._session.flush()
        return run

    async def get_source_state(self, source: str) -> SourceState | None:
        return await self._session.get(SourceState, source)

    async def get_or_create_source_state(self, source: str) -> SourceState:
        state = await self.get_source_state(source)
        if state is None:
            state = SourceState(source=source)
            self._session.add(state)
            await self._session.flush()
        return state

    async def set_watermark(self, source: str, watermark_at: datetime) -> SourceState:
        state = await self.get_or_create_source_state(source)
        state.watermark_at = watermark_at
        await self._session.flush()
        return state

    async def get_unnotified_matches(self, *, min_score: int) -> Sequence[Listing]:
        """Listings with a notifiable LLM match that have not been notified yet.

        Covers this run's new matches and any that a prior run failed to send, so a
        failed notification is retried on the next run.
        """
        stmt = (
            select(Listing)
            .join(Evaluation, Evaluation.listing_id == Listing.id)
            .where(
                Listing.notified_at.is_(None),
                Evaluation.stage == EvaluationStage.LLM,
                Evaluation.verdict == Verdict.MATCH,
                Evaluation.score >= min_score,
            )
            .options(selectinload(Listing.evaluations))
            .order_by(Listing.first_seen_at)
        )
        rows = await self._session.scalars(stmt)
        return rows.unique().all()

    async def mark_notified(self, listings: Iterable[Listing], when: datetime) -> None:
        for listing in listings:
            listing.notified_at = when
        await self._session.flush()

    async def get_listing(self, listing_id: int) -> Listing | None:
        return await self._session.get(Listing, listing_id)

    async def add_application(self, application: Application) -> Application:
        self._session.add(application)
        await self._session.flush()
        return application

    async def get_application(self, application_id: int) -> Application | None:
        return await self._session.get(Application, application_id)

    async def get_application_by_draft_ref(self, draft_ref: str) -> Application | None:
        result = await self._session.scalars(
            select(Application).where(Application.draft_ref == draft_ref)
        )
        return result.first()

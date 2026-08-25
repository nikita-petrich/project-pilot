"""Data-access layer: known-hash lookup, listing upsert, run and state persistence."""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
from sqlalchemy.orm import selectinload

from project_pilot.models import (
    Application,
    ApplicationStatus,
    ContactLead,
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

    def savepoint(self) -> AsyncSessionTransaction:
        """A nested transaction (SAVEPOINT), so one entry's DB failure stays contained."""
        return self._session.begin_nested()

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

    async def record_run_outcome(
        self,
        run_id: int,
        *,
        started_at: datetime,
        status: RunStatus,
        fetched: int = 0,
        new: int = 0,
        evaluated: int = 0,
        matched: int = 0,
        notified: int = 0,
        error: str | None = None,
    ) -> Run:
        """Finalize the run row by id, recreating it if the main unit of work rolled back."""
        run = await self._session.get(Run, run_id)
        if run is None:
            run = Run(started_at=started_at)
            self._session.add(run)
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

    async def get_unnotified_matches(
        self, *, min_score: int, not_before: datetime | None = None
    ) -> Sequence[Listing]:
        """Listings with a notifiable LLM match that have not been notified yet.

        Covers this run's new matches and any that a prior run failed to send, so a
        failed notification is retried on the next run. ``not_before`` bounds the set
        by ``first_seen_at`` so that lowering ``MATCH_THRESHOLD`` (or configuring
        the routine after fire-less runs) does not retro-flood the channel with every
        historical listing that was below the old threshold.
        """
        conditions = [
            Listing.notified_at.is_(None),
            Evaluation.stage == EvaluationStage.LLM,
            Evaluation.verdict == Verdict.MATCH,
            Evaluation.score >= min_score,
        ]
        if not_before is not None:
            conditions.append(Listing.first_seen_at >= not_before)
        stmt = (
            select(Listing)
            .join(Evaluation, Evaluation.listing_id == Listing.id)
            .where(*conditions)
            .options(selectinload(Listing.evaluations))
            .order_by(Listing.first_seen_at)
        )
        rows = await self._session.scalars(stmt)
        return rows.unique().all()

    async def recent_matches(self, *, limit: int = 10) -> Sequence[Listing]:
        """The most recently seen listings with an LLM match verdict, newest first.

        The match feed for the MCP surface: unlike ``get_unnotified_matches`` it
        includes already-notified listings, because the feed is a history, not a
        send queue.
        """
        stmt = (
            select(Listing)
            .join(Evaluation, Evaluation.listing_id == Listing.id)
            .where(
                Evaluation.stage == EvaluationStage.LLM,
                Evaluation.verdict == Verdict.MATCH,
            )
            .options(selectinload(Listing.evaluations))
            .order_by(Listing.first_seen_at.desc())
            .limit(limit)
        )
        rows = await self._session.scalars(stmt)
        return rows.unique().all()

    async def mark_notified(self, listings: Iterable[Listing], when: datetime) -> None:
        for listing in listings:
            listing.notified_at = when
        await self._session.flush()

    async def get_listing(self, listing_id: int) -> Listing | None:
        return await self._session.get(Listing, listing_id)

    async def get_listing_with_evaluations(self, listing_id: int) -> Listing | None:
        """Like ``get_listing`` but with the evaluations eager-loaded.

        Callers that touch ``listing.evaluations`` need this: a lazy load on the
        relationship raises ``MissingGreenlet`` under the async session.
        """
        stmt = (
            select(Listing)
            .where(Listing.id == listing_id)
            .options(selectinload(Listing.evaluations))
        )
        return (await self._session.scalars(stmt)).first()

    async def add_application(self, application: Application) -> Application:
        self._session.add(application)
        await self._session.flush()
        return application

    async def get_application(self, application_id: int) -> Application | None:
        return await self._session.get(Application, application_id)

    async def claim_for_send(self, application_id: int) -> bool:
        """Atomically move a READY application to SENDING; True if this caller won.

        A conditional ``UPDATE ... WHERE status = 'ready'`` is the double-send guard:
        under READ COMMITTED, Postgres re-checks the predicate after taking the row
        lock, so of two concurrent Send clicks exactly one flips the row and the
        other sees zero rows updated (already SENDING) and is refused.
        """
        result = await self._session.execute(
            update(Application)
            .where(
                Application.id == application_id,
                Application.status == ApplicationStatus.READY,
            )
            .values(status=ApplicationStatus.SENDING)
            .returning(Application.id)
        )
        await self._session.flush()
        return result.first() is not None

    async def add_contact_lead(self, lead: ContactLead) -> ContactLead:
        self._session.add(lead)
        await self._session.flush()
        return lead

    async def get_contact_leads(self, listing_id: int) -> Sequence[ContactLead]:
        result = await self._session.scalars(
            select(ContactLead)
            .where(ContactLead.listing_id == listing_id)
            .order_by(ContactLead.created_at.desc())
        )
        return result.all()

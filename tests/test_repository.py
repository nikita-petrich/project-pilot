"""Tests for the Repository data-access layer (skipped when Postgres is absent)."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from project_pilot.models import Evaluation, EvaluationStage, Listing, RunStatus, Verdict
from project_pilot.repository import Repository


def _listing(url_hash: str, *, url: str | None = None, title: str = "T") -> Listing:
    return Listing(
        source="freelancermap",
        external_url=url or f"https://example.test/{url_hash}",
        url_hash=url_hash,
        title=title,
    )


async def test_count_listings_empty(session: AsyncSession) -> None:
    assert await Repository(session).count_listings() == 0


async def test_upsert_inserts_then_touches(session: AsyncSession) -> None:
    repo = Repository(session)
    listing, created = await repo.upsert_listing(_listing("h1"))
    assert created is True
    first_seen = listing.last_seen_at

    again, created_again = await repo.upsert_listing(_listing("h1"))
    assert created_again is False
    assert again.id == listing.id
    assert again.last_seen_at >= first_seen
    assert await repo.count_listings() == 1


async def test_get_known_hashes(session: AsyncSession) -> None:
    repo = Repository(session)
    await repo.upsert_listing(_listing("a"))
    await repo.upsert_listing(_listing("b"))
    assert await repo.get_known_hashes(["a", "b", "c"]) == {"a", "b"}


async def test_get_known_hashes_empty_input(session: AsyncSession) -> None:
    assert await Repository(session).get_known_hashes([]) == set()


async def test_add_evaluation(session: AsyncSession) -> None:
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("h"))
    evaluation = await repo.add_evaluation(
        Evaluation(
            listing_id=listing.id,
            stage=EvaluationStage.HARD_RULE,
            verdict=Verdict.NO_MATCH,
            reason={"rule": "blacklist", "matched_term": "wordpress"},
        )
    )
    assert evaluation.id is not None
    assert evaluation.listing_id == listing.id
    assert evaluation.reason["rule"] == "blacklist"


async def test_record_run(session: AsyncSession) -> None:
    repo = Repository(session)
    run = await repo.start_run()
    assert run.id is not None
    assert run.finished_at is None

    finalized = await repo.finalize_run(
        run,
        status=RunStatus.SUCCESS,
        fetched=4,
        new=2,
        evaluated=1,
        matched=1,
        notified=1,
    )
    assert finalized.status == RunStatus.SUCCESS
    assert finalized.finished_at is not None
    assert finalized.fetched == 4


async def test_watermark_roundtrip(session: AsyncSession) -> None:
    repo = Repository(session)
    assert await repo.get_source_state("freelancermap") is None

    when = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    state = await repo.set_watermark("freelancermap", when)
    assert state.watermark_at == when

    fetched = await repo.get_source_state("freelancermap")
    assert fetched is not None
    assert fetched.watermark_at == when

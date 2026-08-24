"""Tests for the Repository data-access layer (skipped when Postgres is absent)."""

from datetime import UTC, datetime, timedelta

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

    finalized = await repo.record_run_outcome(
        run.id,
        started_at=run.started_at,
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


async def _add_llm_match(
    repo: Repository, url_hash: str, *, first_seen_at: datetime, score: int = 80
) -> None:
    listing = _listing(url_hash)
    listing.first_seen_at = first_seen_at
    listing.last_seen_at = first_seen_at
    stored, _ = await repo.upsert_listing(listing)
    await repo.add_evaluation(
        Evaluation(
            listing_id=stored.id,
            stage=EvaluationStage.LLM,
            verdict=Verdict.MATCH,
            score=score,
            reason={"reasons": ["fit"]},
        )
    )


async def test_unnotified_matches_recency_bound(session: AsyncSession) -> None:
    """A recency bound keeps a lowered threshold from resurfacing ancient matches."""
    repo = Repository(session)
    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    await _add_llm_match(repo, "recent", first_seen_at=now - timedelta(hours=1))
    await _add_llm_match(repo, "ancient", first_seen_at=now - timedelta(days=30))

    unbounded = await repo.get_unnotified_matches(min_score=60)
    assert {listing.url_hash for listing in unbounded} == {"recent", "ancient"}

    bounded = await repo.get_unnotified_matches(min_score=60, not_before=now - timedelta(days=2))
    assert {listing.url_hash for listing in bounded} == {"recent"}


async def test_record_thread_stores_the_topic_and_reads_back(session: AsyncSession) -> None:
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t1"))

    thread = await repo.record_thread(listing.id, 4711)
    assert thread.thread_id == 4711

    found = await repo.get_thread(listing.id)
    assert found is not None
    assert found.thread_id == 4711


async def test_get_thread_is_none_before_a_topic_exists(session: AsyncSession) -> None:
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t2"))
    assert await repo.get_thread(listing.id) is None


async def test_record_thread_is_idempotent_per_listing(session: AsyncSession) -> None:
    # A rerun must not open a second topic for the same project: the unique
    # constraint would otherwise fail the whole run rather than this one listing.
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t3"))

    first = await repo.record_thread(listing.id, 100)
    second = await repo.record_thread(listing.id, 200)

    assert second.id == first.id
    assert second.thread_id == 100  # the first topic wins; the second is ignored


async def test_thread_is_findable_by_its_telegram_thread_id(session: AsyncSession) -> None:
    # Incoming messages carry only the thread id; that is the routing key.
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t4"))
    await repo.record_thread(listing.id, 5150)

    found = await repo.get_thread_by_thread_id(5150)
    assert found is not None
    assert found.listing_id == listing.id
    assert await repo.get_thread_by_thread_id(999) is None


async def test_append_history_keeps_only_the_last_turns(session: AsyncSession) -> None:
    # Bounded on write so a long-running topic cannot grow the row without limit.
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t5"))
    thread = await repo.record_thread(listing.id, 5151)

    await repo.append_history(
        thread, [{"role": "user", "text": "eins"}, {"role": "assistant", "text": "zwei"}], keep=3
    )
    await repo.append_history(
        thread, [{"role": "user", "text": "drei"}, {"role": "assistant", "text": "vier"}], keep=3
    )

    stored = await repo.get_thread_by_thread_id(5151)
    assert stored is not None
    assert [turn["text"] for turn in stored.history] == ["zwei", "drei", "vier"]
    assert stored.updated_at >= stored.created_at

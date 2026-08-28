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


async def test_record_channel_message_stores_the_post_and_reads_back(
    session: AsyncSession,
) -> None:
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t1"))

    thread = await repo.record_channel_message(listing.id, 4711)
    assert thread.channel_message_id == 4711
    assert thread.thread_id is None  # not until Telegram forwards the post

    found = await repo.get_thread(listing.id)
    assert found is not None
    assert found.channel_message_id == 4711


async def test_get_thread_is_none_before_a_card_exists(session: AsyncSession) -> None:
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t2"))
    assert await repo.get_thread(listing.id) is None


async def test_record_channel_message_is_idempotent_per_listing(
    session: AsyncSession,
) -> None:
    # A rerun must not post a second card for the same project: the unique
    # constraint would otherwise fail the whole run rather than this one listing.
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t3"))

    first = await repo.record_channel_message(listing.id, 100)
    second = await repo.record_channel_message(listing.id, 200)

    assert second.id == first.id
    assert second.channel_message_id == 100  # the first post wins


async def test_a_conversation_that_gained_a_listing_can_still_gain_a_card(
    session: AsyncSession,
) -> None:
    # The agent binds a listing to a thread you started; the card is posted
    # afterwards, and has to land on that same row rather than a second one.
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t3b"))
    thread = await repo.ensure_thread(7000)
    assert await repo.set_listing_id(thread, listing.id) is True

    same = await repo.record_channel_message(listing.id, 900)

    assert same.id == thread.id
    assert same.channel_message_id == 900
    assert same.thread_id == 7000


async def test_a_card_is_findable_by_the_id_of_its_channel_post(
    session: AsyncSession,
) -> None:
    # A button press and the automatic forward both name only the post's id.
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t4"))
    await repo.record_channel_message(listing.id, 5150)

    found = await repo.get_thread_by_channel_message(5150)
    assert found is not None
    assert found.listing_id == listing.id
    assert await repo.get_thread_by_channel_message(999) is None


async def test_binding_the_comment_thread_is_recorded_once(session: AsyncSession) -> None:
    # The automatic forward can be redelivered; the second one must be harmless.
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t4b"))
    thread = await repo.record_channel_message(listing.id, 800)

    assert await repo.bind_thread_id(thread, 801) is True
    assert await repo.bind_thread_id(thread, 801) is True  # same root, still fine
    assert await repo.bind_thread_id(thread, 999) is False  # a different one is not

    found = await repo.get_thread_by_thread_id(801)
    assert found is not None
    assert found.listing_id == listing.id


async def test_a_root_already_claimed_is_not_taken_from_its_conversation(
    session: AsyncSession,
) -> None:
    # Two rows on one thread id would fail the poll round on the unique index.
    repo = Repository(session)
    first, _ = await repo.upsert_listing(_listing("t4c"))
    second, _ = await repo.upsert_listing(_listing("t4d"))
    one = await repo.record_channel_message(first.id, 810)
    two = await repo.record_channel_message(second.id, 811)
    assert await repo.bind_thread_id(one, 812) is True

    assert await repo.bind_thread_id(two, 812) is False
    assert two.thread_id is None


async def test_deleting_a_thread_removes_it_from_every_lookup(
    session: AsyncSession,
) -> None:
    # Declining a match must leave nothing behind that says it still has a card.
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t4e"))
    thread = await repo.record_channel_message(listing.id, 820)
    await repo.bind_thread_id(thread, 821)

    await repo.delete_thread(thread)

    assert await repo.get_thread(listing.id) is None
    assert await repo.get_thread_by_channel_message(820) is None
    assert await repo.get_thread_by_thread_id(821) is None


async def test_the_session_id_of_a_thread_is_written_and_replaced(
    session: AsyncSession,
) -> None:
    # Replaced, not appended to: the SDK hands back a new id whenever it could
    # not resume the old one, and a stale id would restart the thread every time.
    repo = Repository(session)
    listing, _ = await repo.upsert_listing(_listing("t5"))
    thread = await repo.record_channel_message(listing.id, 5151)
    await repo.bind_thread_id(thread, 5152)
    assert thread.session_id is None

    await repo.set_session_id(thread, "sess-1")
    await repo.set_session_id(thread, "sess-2")

    stored = await repo.get_thread_by_thread_id(5152)
    assert stored is not None
    assert stored.session_id == "sess-2"
    assert stored.updated_at >= stored.created_at


async def test_ensure_thread_opens_a_listingless_conversation(session: AsyncSession) -> None:
    # A thread you started still needs a row: that is where its session lives.
    repo = Repository(session)

    opened = await repo.ensure_thread(4321)
    again = await repo.ensure_thread(4321)

    assert opened.id == again.id  # idempotent, one conversation per thread
    assert opened.listing_id is None
    await repo.set_session_id(opened, "sess-open")
    stored = await repo.get_thread_by_thread_id(4321)
    assert stored is not None
    assert stored.session_id == "sess-open"


async def test_several_listingless_topics_can_coexist(session: AsyncSession) -> None:
    # Postgres allows any number of nulls under the unique constraint, so the
    # guard against two topics for one match does not block these.
    repo = Repository(session)

    first = await repo.ensure_thread(11)
    second = await repo.ensure_thread(22)

    assert first.id != second.id
    assert (first.listing_id, second.listing_id) == (None, None)

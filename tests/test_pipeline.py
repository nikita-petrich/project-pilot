"""Integration tests for the pipeline (real Postgres, fixtures, fake clients)."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.config import SOURCE_NAME, Settings
from project_pilot.errors import SourceBlockedError, SourceUnavailableError
from project_pilot.evaluation.llm import LlmEvaluation, LlmProbe
from project_pilot.evaluation.schemas import MatchVerdict
from project_pilot.health import HealthKind, llm_issue
from project_pilot.ingestion.normalize import compute_url_hash
from project_pilot.models import Evaluation, EvaluationStage, Listing, ListingStatus, Run, RunStatus
from project_pilot.notification.messages import MatchMessage
from project_pilot.pipeline import Matcher, Pipeline, SourceClient
from project_pilot.profile_loader import Profile, ProfileConstraints
from project_pilot.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = "https://www.freelancermap.de/projekte"
DETAIL1 = "https://www.freelancermap.de/projekt/senior-python-entwickler-backend-12345"
DETAIL2 = "https://www.freelancermap.de/projekt/data-engineer-azure-67890"
NOW = datetime(2026, 7, 21, 7, 20, tzinfo=UTC)  # ~8 min after card1's posted 07:12 UTC
MODEL = "gpt-tiny-42"

LIST_HTML = (
    '<script class="js-react-on-rails-component" data-component-name="ProjectSearch">'
    '{"currentPage":1,"initialResults":['
    '{"id":12345,"slug":"senior-python-entwickler-backend-12345",'
    '"title":"Senior Python","created":"2026-07-21T09:12:00+02:00"},'
    '{"id":67890,"slug":"data-engineer-azure-67890",'
    '"title":"Data Engineer","created":"2026-07-21T09:15:00+02:00"}'
    "]}</script>"
)
# Pagination walks by incrementing pagenr; page 2 is an empty result set (end).
LIST_HTML_PAGE2 = (
    '<script class="js-react-on-rails-component" data-component-name="ProjectSearch">'
    '{"currentPage":2,"initialResults":[]}</script>'
)
SEARCH_PAGE2 = SEARCH + "?pagenr=2"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


PAGES = {
    SEARCH: LIST_HTML,
    SEARCH_PAGE2: LIST_HTML_PAGE2,
    DETAIL1: _fixture("freelancermap_detail_asap_remote.html"),
    DETAIL2: _fixture("freelancermap_detail_dated_onsite.html"),
}


@dataclass
class _Resp:
    text: str


class _FakeClient:
    def __init__(self, pages: dict[str, str], *, block: bool = False) -> None:
        self._pages = pages
        self._block = block

    async def check_robots(self, urls: list[str]) -> None:
        return None

    async def get(self, url: str) -> _Resp:
        if self._block:
            raise SourceBlockedError(f"HTTP 403 for {url}")
        return _Resp(self._pages[url])

    async def aclose(self) -> None:
        return None


class _FakeMatcher:
    async def evaluate(self, *, profile_text: str, listing_text: str) -> LlmEvaluation:
        matched = "asyncio" in listing_text.lower()
        verdict = MatchVerdict(
            project_title="Python-Projekt",
            verdict="match" if matched else "no_match",
            score=80 if matched else 30,
            reasons=["strong fit"] if matched else ["weak fit"],
            matching_skills=["python"] if matched else [],
            missing_requirements=[],
            risk_flags=[],
        )
        return LlmEvaluation(
            verdict=verdict,
            model="fake",
            prompt_version="test",
            tokens_in=10,
            tokens_out=5,
            latency_ms=1,
            is_error=False,
        )


class _FakeNotifier:
    """The channel fake: posts and warnings record, delivery is switchable."""

    def __init__(self, *, delivers: bool = True) -> None:
        self.delivers = delivers
        self.matches: list[MatchMessage] = []
        self.warnings: list[str] = []
        self.posted: list[int] = []
        self._next_post = 1000

    async def notify(self, message: MatchMessage) -> int | None:
        self.matches.append(message)
        if not self.delivers:
            return None
        self._next_post += 1
        self.posted.append(self._next_post)
        return self._next_post

    async def notify_warning(self, text: str) -> bool:
        self.warnings.append(text)
        return self.delivers


def _settings() -> Settings:
    return Settings(
        search_urls=[SEARCH],
        match_threshold=60,
        analysis_window_min=30,
        llm_model=MODEL,
    )


def _profile(blacklist: list[str] | None = None) -> Profile:
    return Profile(
        text="Python engineer wanting remote async backend work.",
        constraints=ProfileConstraints(
            blacklist=blacklist if blacklist is not None else ["wordpress"],
            must_have=[],
            languages=["de", "en"],
        ),
        profile_hash="testhash",
    )


def _pipeline(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client: SourceClient,
    matcher: Matcher | None = None,
    profile: Profile | None = None,
    llm_probe: LlmProbe | None = None,
    notifier: "_FakeNotifier | _CommitProbeNotifier | None" = None,
) -> Pipeline:
    return Pipeline(
        settings=_settings(),
        profile=profile or _profile(),
        session_factory=session_factory,
        client_factory=lambda: client,
        matcher=matcher or _FakeMatcher(),
        llm_probe=llm_probe,
        notifier=notifier,
    )


async def _seed_state(
    session_factory: async_sessionmaker[AsyncSession], *, watermark: datetime
) -> None:
    async with session_factory() as db_session:
        repo = Repository(db_session)
        await repo.upsert_listing(
            Listing(
                source=SOURCE_NAME,
                external_url="https://x/seed",
                url_hash="seedhash",
                title="seed",
            )
        )
        await repo.set_watermark(SOURCE_NAME, watermark)
        await db_session.commit()


async def test_seed_run_persists_without_analysis(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    notifier = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=notifier)
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.is_seed is True
    assert outcome.new == 2
    assert outcome.notified == 0
    assert notifier.matches == []
    async with session_factory() as db_session:
        listings = (await db_session.scalars(select(Listing))).all()
        assert len(listings) == 2
        assert all(listing.status is ListingStatus.SKIPPED_STALE for listing in listings)


async def test_full_run_notifies_match(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=notifier)
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.is_seed is False
    assert outcome.new == 2
    assert outcome.evaluated == 2
    assert outcome.matched == 1
    assert outcome.notified == 1
    assert len(notifier.matches) == 1
    assert "Senior Python" in notifier.matches[0].title
    async with session_factory() as db_session:
        repo = Repository(db_session)
        card1 = await repo.get_listing_by_hash(compute_url_hash(DETAIL1))
        card2 = await repo.get_listing_by_hash(compute_url_hash(DETAIL2))
        assert card1 is not None
        assert card1.notified_at is not None
        assert card2 is not None
        assert card2.notified_at is None


async def test_match_fires_routine_and_stores_session_url(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    fire = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=fire)
    await pipeline.run_once(now=NOW)
    assert len(fire.matches) == 1
    async with session_factory() as db_session:
        listing = await Repository(db_session).get_listing_by_hash(compute_url_hash(DETAIL1))
        assert listing is not None
        assert listing.notified_at is not None

    # A later run must not push again for the same listing; notified_at is the guard.
    second_fire = _FakeNotifier()
    again = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=second_fire)
    await again.run_once(now=NOW + timedelta(minutes=15))
    assert second_fire.matches == []


async def test_failed_push_never_fails_the_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    fire = _FakeNotifier(delivers=False)  # ntfy unreachable / misconfigured
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=fire)
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.notified == 0  # nothing delivered, run itself stays green
    assert len(fire.matches) == 1
    async with session_factory() as db_session:
        listing = await Repository(db_session).get_listing_by_hash(compute_url_hash(DETAIL1))
        assert listing is not None
        assert listing.notified_at is None  # stays pending, retried next run


async def test_per_entry_isolation_skips_failing_detail(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pages = {
        SEARCH: LIST_HTML,
        SEARCH_PAGE2: LIST_HTML_PAGE2,
        DETAIL1: PAGES[DETAIL1],
    }  # DETAIL2 absent
    pipeline = _pipeline(session_factory, client=_FakeClient(pages))
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.new == 1
    assert outcome.errors == 1
    assert outcome.status is RunStatus.PARTIAL


async def test_source_blocked_marks_run_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES, block=True))
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.is_error is True
    assert outcome.status is RunStatus.ERROR


def _endless_list_html(pagenr: int) -> str:
    slug = f"endless-{pagenr}"
    return (
        '<script class="js-react-on-rails-component" data-component-name="ProjectSearch">'
        f'{{"currentPage":{pagenr},"initialResults":[{{"id":{1000 + pagenr},'
        f'"slug":"{slug}","title":"Endless {pagenr}","created":"2026-07-21T09:12:00+02:00"}}]}}'
        "</script>"
    )


def _endless_detail_html(slug: str) -> str:
    return (
        '<script class="js-react-on-rails-component" data-component-name="ProjectShow">'
        f'{{"project":{{"slug":"{slug}","title":"{slug}",'
        '"created":"2026-07-21T09:12:00+02:00","startText":"ab sofort","city":"Remote",'
        '"country":{"nameDe":"Deutschland"},"contractType":{"remoteInPercent":100},'
        '"skills":{"enabled":[{"localizedName":"Python"}]},'
        '"description":"<p>Endless.</p>"}}</script>'
    )


class _EndlessClient:
    """Every list page holds a brand-new listing, so pagination never stops early."""

    def __init__(self) -> None:
        self.list_pages_fetched = 0

    async def check_robots(self, urls: list[str]) -> None:
        return None

    async def get(self, url: str) -> _Resp:
        if "/projekt/" in url:
            return _Resp(_endless_detail_html(url.rsplit("/", 1)[-1]))
        pagenr = 1
        _, _, query = url.partition("pagenr=")
        if query:
            pagenr = int(query.split("&", 1)[0])
        self.list_pages_fetched += 1
        return _Resp(_endless_list_html(pagenr))

    async def aclose(self) -> None:
        return None


async def test_pagination_cap_is_surfaced_and_holds_watermark(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A backlog deeper than the page cap must not silently advance the watermark."""
    from project_pilot.pipeline import MAX_LIST_PAGES

    watermark = NOW - timedelta(minutes=10)
    await _seed_state(session_factory, watermark=watermark)
    client = _EndlessClient()
    pipeline = _pipeline(session_factory, client=client)
    outcome = await pipeline.run_once(now=NOW)
    # The loop stopped at the cap, not on a known/older listing.
    assert outcome.pagination_truncated is True
    assert client.list_pages_fetched == MAX_LIST_PAGES
    assert outcome.new == MAX_LIST_PAGES
    # Watermark held at its old value so the next run re-collects the backlog.
    async with session_factory() as db_session:
        state = await Repository(db_session).get_source_state(SOURCE_NAME)
        assert state is not None
        assert state.watermark_at == watermark


async def test_notification_retry_on_next_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    failing = _FakeNotifier(delivers=False)
    first = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=failing)
    outcome1 = await first.run_once(now=NOW)
    assert outcome1.matched == 1
    assert outcome1.notified == 0
    assert len(failing.matches) == 1

    ok_notifier = _FakeNotifier()
    second = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=ok_notifier)
    outcome2 = await second.run_once(now=NOW + timedelta(minutes=15))
    assert outcome2.new == 0
    assert outcome2.notified == 1
    assert len(ok_notifier.matches) == 1


async def test_second_run_finds_no_new(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = _pipeline(session_factory, client=_FakeClient(PAGES))
    await first.run_once(now=NOW)
    second = _pipeline(session_factory, client=_FakeClient(PAGES))
    outcome = await second.run_once(now=NOW + timedelta(minutes=15))
    assert outcome.new == 0


async def test_stale_listings_are_skipped_not_analysed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    late = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)  # far past card1's posted time
    await _seed_state(session_factory, watermark=late - timedelta(minutes=120))
    notifier = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=notifier)
    outcome = await pipeline.run_once(now=late)
    assert outcome.matched == 0
    assert outcome.notified == 0
    assert notifier.matches == []
    async with session_factory() as db_session:
        repo = Repository(db_session)
        card1 = await repo.get_listing_by_hash(compute_url_hash(DETAIL1))
        assert card1 is not None
        assert card1.status is ListingStatus.SKIPPED_STALE


async def test_blacklist_rejects_before_llm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier()
    pipeline = _pipeline(
        session_factory,
        client=_FakeClient(PAGES),
        notifier=notifier,
        profile=_profile(blacklist=["asyncio"]),
    )
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.matched == 0
    assert notifier.matches == []


async def test_dry_run_without_fire_does_not_notify(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=None)
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.matched == 1
    assert outcome.notified == 0
    async with session_factory() as db_session:
        repo = Repository(db_session)
        card1 = await repo.get_listing_by_hash(compute_url_hash(DETAIL1))
        assert card1 is not None
        assert card1.notified_at is None


class _BrokenClient:
    async def check_robots(self, urls: list[str]) -> None:
        return None

    async def get(self, url: str) -> _Resp:
        raise RuntimeError("boom")

    async def aclose(self) -> None:
        return None


class _OfflineClient:
    """Stands in for a dropped home connection: robots.txt never resolves."""

    async def check_robots(self, urls: list[str]) -> None:
        raise SourceUnavailableError(
            "https://www.freelancermap.de/robots.txt unreachable after 3 attempts "
            "(ConnectError: All connection attempts failed)"
        )

    async def get(self, url: str) -> _Resp:
        raise AssertionError("unreachable")  # pragma: no cover

    async def aclose(self) -> None:
        return None


async def test_unreachable_source_fails_run_without_cooldown_or_traceback(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    watermark = NOW - timedelta(minutes=10)
    await _seed_state(session_factory, watermark=watermark)
    notifier = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_OfflineClient(), notifier=notifier)

    with caplog.at_level(logging.WARNING, logger="project_pilot.pipeline"):
        outcome = await pipeline.run_once(now=NOW)

    assert outcome.is_error is True
    assert outcome.error is not None
    assert outcome.error.startswith("source unreachable:")
    records = [record for record in caplog.records if record.name == "project_pilot.pipeline"]
    assert records and all(record.exc_info is None for record in records)  # no traceback dump
    assert notifier.warnings == []
    async with session_factory() as db_session:
        state = await Repository(db_session).get_source_state(SOURCE_NAME)
        assert state is not None
        assert state.cooldown_until is None  # only 403/captcha cools down
        assert state.consecutive_failures == 1
        assert state.watermark_at == watermark  # gap stays open for the next run


async def test_source_blocked_sets_cooldown_and_warns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    warns = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES, block=True), notifier=warns)
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.is_error is True
    assert any("Cooling down" in message for message in warns.warnings)
    async with session_factory() as db_session:
        state = await Repository(db_session).get_source_state(SOURCE_NAME)
        assert state is not None
        assert state.cooldown_until is not None
        assert state.cooldown_until > NOW


async def test_cooldown_skips_next_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = _pipeline(
        session_factory, client=_FakeClient(PAGES, block=True), notifier=_FakeNotifier()
    )
    await first.run_once(now=NOW)

    second = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=_FakeNotifier())
    outcome = await second.run_once(now=NOW + timedelta(minutes=15))
    assert outcome.error == "skipped: in cooldown"
    async with session_factory() as db_session:
        assert await Repository(db_session).count_listings() == 0


async def test_three_consecutive_failures_warn_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    warns = _FakeNotifier()
    for index in range(3):
        pipeline = Pipeline(
            settings=_settings(),
            profile=_profile(),
            session_factory=session_factory,
            client_factory=_BrokenClient,
            matcher=_FakeMatcher(),
            notifier=warns,
        )
        outcome = await pipeline.run_once(now=NOW + timedelta(minutes=15 * index))
        assert outcome.is_error is True

    failure_warnings = [m for m in warns.warnings if "consecutive failed runs" in m]
    assert len(failure_warnings) == 1


class _MatchAllMatcher:
    """Matches every listing at score 80 (drives the on-site suppression test)."""

    async def evaluate(self, *, profile_text: str, listing_text: str) -> LlmEvaluation:
        return LlmEvaluation(
            verdict=MatchVerdict(
                project_title="Python-Projekt",
                verdict="match",
                score=80,
                reasons=["fits"],
                matching_skills=["python"],
                missing_requirements=[],
                risk_flags=[],
            ),
            model="fake",
            prompt_version="test",
            tokens_in=10,
            tokens_out=5,
            latency_ms=1,
            is_error=False,
        )


class _CommitProbeNotifier:
    """Checks from a separate session that the evaluation is already committed."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.saw_committed: list[bool] = []

    async def notify(self, message: MatchMessage) -> int | None:
        async with self._session_factory() as probe:
            row = await probe.scalar(
                select(Evaluation)
                .join(Listing, Listing.id == Evaluation.listing_id)
                .where(
                    Listing.external_url == message.url,
                    Evaluation.stage == EvaluationStage.LLM,
                )
            )
            self.saw_committed.append(row is not None)
        return 1

    async def notify_warning(self, text: str) -> bool:
        return True


async def test_watermark_held_on_detail_error_and_gap_closed_next_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    watermark = NOW - timedelta(minutes=10)
    await _seed_state(session_factory, watermark=watermark)
    pages = {SEARCH: LIST_HTML, SEARCH_PAGE2: LIST_HTML_PAGE2, DETAIL1: PAGES[DETAIL1]}
    outcome = await _pipeline(session_factory, client=_FakeClient(pages)).run_once(now=NOW)
    assert outcome.status is RunStatus.PARTIAL
    async with session_factory() as db_session:
        state = await Repository(db_session).get_source_state(SOURCE_NAME)
        assert state is not None
        assert state.watermark_at == watermark  # held: DETAIL2 was never stored

    second = await _pipeline(session_factory, client=_FakeClient(PAGES)).run_once(
        now=NOW + timedelta(minutes=15)
    )
    assert second.new == 1  # the failed listing is re-collected, nothing is lost
    async with session_factory() as db_session:
        repo = Repository(db_session)
        assert await repo.get_listing_by_hash(compute_url_hash(DETAIL2)) is not None
        state = await repo.get_source_state(SOURCE_NAME)
        assert state is not None
        assert state.watermark_at == NOW + timedelta(minutes=15)


async def test_db_error_on_one_listing_is_contained_and_run_recorded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    long_slug = "x" * 1100  # canonical URL exceeds the 1024-char column: INSERT fails
    long_url = f"https://www.freelancermap.de/projekt/{long_slug}"
    list_html = (
        '<script class="js-react-on-rails-component" data-component-name="ProjectSearch">'
        '{"currentPage":1,"initialResults":['
        '{"id":1,"slug":"'
        + long_slug
        + '","title":"Broken","created":"2026-07-21T09:12:00+02:00"},'
        '{"id":12345,"slug":"senior-python-entwickler-backend-12345",'
        '"title":"Senior Python","created":"2026-07-21T09:12:00+02:00"}'
        "]}</script>"
    )
    pages = {
        SEARCH: list_html,
        SEARCH_PAGE2: LIST_HTML_PAGE2,
        DETAIL1: PAGES[DETAIL1],
        long_url: PAGES[DETAIL1],
    }
    outcome = await _pipeline(session_factory, client=_FakeClient(pages)).run_once(now=NOW)
    assert outcome.errors == 1
    assert outcome.new == 1  # the healthy listing was stored despite the DB error
    assert outcome.status is RunStatus.PARTIAL
    async with session_factory() as db_session:
        repo = Repository(db_session)
        assert await repo.get_listing_by_hash(compute_url_hash(DETAIL1)) is not None
        runs = (await db_session.scalars(select(Run))).all()
        assert len(runs) == 1  # the run row survived the poisoned-entry flush error
        assert runs[0].status is RunStatus.PARTIAL
        state = await repo.get_source_state(SOURCE_NAME)
        assert state is not None
        assert state.consecutive_failures == 0


async def test_evaluations_are_committed_before_notification(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    probe = _CommitProbeNotifier(session_factory)
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=probe)
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.notified == 1
    assert probe.saw_committed == [True]  # the send saw durable state, not a dirty session


async def test_onsite_only_match_is_suppressed_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier()
    pipeline = Pipeline(
        settings=_settings(),
        profile=_profile(),
        session_factory=session_factory,
        client_factory=lambda: _FakeClient(PAGES),
        matcher=_MatchAllMatcher(),
        notifier=notifier,
    )
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.matched == 2
    assert outcome.notified == 1  # the on-site-only match is suppressed, not sent
    assert len(notifier.matches) == 1
    async with session_factory() as db_session:
        repo = Repository(db_session)
        onsite = await repo.get_listing_by_hash(compute_url_hash(DETAIL2))
        assert onsite is not None
        assert onsite.notified_at is not None  # marked handled: leaves the pending set

    second_notifier = _FakeNotifier()
    second = Pipeline(
        settings=_settings(),
        profile=_profile(),
        session_factory=session_factory,
        client_factory=lambda: _FakeClient(PAGES),
        matcher=_MatchAllMatcher(),
        notifier=second_notifier,
    )
    await second.run_once(now=NOW + timedelta(minutes=15))
    assert second_notifier.matches == []  # nothing pending anymore


async def test_long_title_and_location_are_truncated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    slug = "long-title-1"
    url = f"https://www.freelancermap.de/projekt/{slug}"
    detail = (
        '<script class="js-react-on-rails-component" data-component-name="ProjectShow">'
        + json.dumps(
            {
                "project": {
                    "title": "T" * 600,
                    "created": "2026-07-21T09:12:00+02:00",
                    "city": "C" * 300,
                    "description": "Python asyncio backend",
                }
            }
        )
        + "</script>"
    )
    list_html = (
        '<script class="js-react-on-rails-component" data-component-name="ProjectSearch">'
        '{"currentPage":1,"initialResults":[{"id":2,"slug":"' + slug + '",'
        '"title":"Long","created":"2026-07-21T09:12:00+02:00"}]}</script>'
    )
    pages = {SEARCH: list_html, SEARCH_PAGE2: LIST_HTML_PAGE2, url: detail}
    outcome = await _pipeline(session_factory, client=_FakeClient(pages)).run_once(now=NOW)
    assert outcome.errors == 0
    async with session_factory() as db_session:
        stored = await Repository(db_session).get_listing_by_hash(compute_url_hash(url))
        assert stored is not None
        assert len(stored.title) == 512
        assert stored.location is not None
        assert len(stored.location) == 256


class _BrokenMatcher:
    """Stage 3 down: the fallback verdict every listing gets when the LLM cannot answer."""

    def __init__(self, kind: HealthKind = HealthKind.MODEL_NOT_FOUND) -> None:
        self.kind = kind

    async def evaluate(self, *, profile_text: str, listing_text: str) -> LlmEvaluation:
        issue = llm_issue(self.kind, model=MODEL, detail="404 model_not_found")
        return LlmEvaluation(
            verdict=MatchVerdict.llm_error_fallback(issue.detail),
            model=MODEL,
            prompt_version="test",
            tokens_in=None,
            tokens_out=None,
            latency_ms=1,
            is_error=True,
            issue=issue,
        )


class _FakeProbe:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls = 0

    async def ping(self, *, model: str) -> None:
        self.calls += 1
        if self._error is not None:
            raise self._error


async def test_a_broken_llm_is_announced_instead_of_passing_as_a_clean_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every listing scored `llm_error` still records a successful run — say it out loud."""
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier()
    pipeline = _pipeline(
        session_factory,
        client=_FakeClient(PAGES),
        matcher=_BrokenMatcher(),
        notifier=notifier,
    )

    outcome = await pipeline.run_once(now=NOW)

    assert outcome.is_error is False  # the run itself succeeded, which is the trap
    assert outcome.llm_errors == 2
    assert outcome.llm_ok == 0
    assert outcome.notified == 0
    assert len(notifier.warnings) == 1
    assert MODEL in notifier.warnings[0]
    assert "LLM_MODEL" in notifier.warnings[0]


async def test_the_same_broken_llm_is_not_re_announced_every_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier()
    pipeline = _pipeline(
        session_factory,
        client=_FakeClient(PAGES),
        matcher=_BrokenMatcher(),
        notifier=notifier,
    )

    await pipeline.run_once(now=NOW)
    await pipeline.report_llm_issue(
        llm_issue(HealthKind.MODEL_NOT_FOUND, model=MODEL), now=NOW + timedelta(minutes=15)
    )

    assert len(notifier.warnings) == 1


async def test_a_repaired_llm_is_announced_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier()
    pipeline = _pipeline(
        session_factory,
        client=_FakeClient(PAGES),
        matcher=_BrokenMatcher(),
        notifier=notifier,
    )

    await pipeline.run_once(now=NOW)
    await pipeline.report_llm_issue(None, now=NOW + timedelta(minutes=15))
    await pipeline.report_llm_issue(None, now=NOW + timedelta(minutes=30))

    assert len(notifier.warnings) == 2
    assert notifier.warnings[1].startswith("✅")


async def test_a_healthy_run_says_nothing_about_the_llm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=notifier)

    outcome = await pipeline.run_once(now=NOW)

    assert outcome.llm_ok == 2
    assert outcome.llm_errors == 0
    assert notifier.warnings == []


async def test_preflight_reports_a_broken_model_before_any_scan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    notifier = _FakeNotifier()
    probe = _FakeProbe(RuntimeError("Error code: 404 - model does not exist"))
    pipeline = _pipeline(
        session_factory, client=_FakeClient(PAGES), notifier=notifier, llm_probe=probe
    )

    issue = await pipeline.check_llm(now=NOW)

    assert probe.calls == 1
    assert issue is not None
    assert len(notifier.warnings) == 1
    assert MODEL in notifier.warnings[0]


async def test_preflight_is_silent_when_the_model_answers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    notifier = _FakeNotifier()
    pipeline = _pipeline(
        session_factory, client=_FakeClient(PAGES), notifier=notifier, llm_probe=_FakeProbe()
    )

    assert await pipeline.check_llm(now=NOW) is None
    assert notifier.warnings == []


async def test_each_match_gets_a_channel_post_and_the_post_id_is_stored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=notifier)
    await pipeline.run_once(now=NOW)

    assert len(notifier.posted) == len(notifier.matches)

    async with session_factory() as db_session:
        repo = Repository(db_session)
        listing = await repo.get_listing_by_hash(compute_url_hash(DETAIL1))
        assert listing is not None
        thread = await repo.get_thread(listing.id)
        assert thread is not None
        # The id of the post is the only handle onto the comment thread
        # Telegram is about to open; losing it would strand the conversation.
        assert thread.channel_message_id == notifier.posted[0]
        assert thread.thread_id is None  # not until the automatic forward lands


async def test_a_failed_send_stores_nothing_and_retries_next_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # No post exists, so no row may claim one: the retry has to be free to post.
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    failing = _FakeNotifier(delivers=False)
    first = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=failing)
    outcome = await first.run_once(now=NOW)
    assert outcome.notified == 0

    async with session_factory() as db_session:
        repo = Repository(db_session)
        listing = await repo.get_listing_by_hash(compute_url_hash(DETAIL1))
        assert listing is not None
        assert await repo.get_thread(listing.id) is None

    ok = _FakeNotifier()
    second = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=ok)
    assert (await second.run_once(now=NOW + timedelta(minutes=15))).notified == 1

    async with session_factory() as db_session:
        repo = Repository(db_session)
        listing = await repo.get_listing_by_hash(compute_url_hash(DETAIL1))
        assert listing is not None
        thread = await repo.get_thread(listing.id)
        assert thread is not None
        assert thread.channel_message_id == ok.posted[0]


async def test_a_match_already_posted_is_never_posted_twice(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # notified_at is what stops it, and the row keeps the first post's id.
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), notifier=notifier)
    await pipeline.run_once(now=NOW)
    posted_once = list(notifier.posted)

    await pipeline.run_once(now=NOW + timedelta(minutes=15))

    assert notifier.posted == posted_once

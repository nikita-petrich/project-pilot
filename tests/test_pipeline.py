"""Integration tests for the pipeline (real Postgres, fixtures, fake clients)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.config import SOURCE_NAME, Settings
from project_pilot.errors import SourceBlockedError
from project_pilot.evaluation.llm import LlmEvaluation
from project_pilot.evaluation.schemas import MatchVerdict
from project_pilot.ingestion.normalize import compute_url_hash
from project_pilot.models import Listing, ListingStatus, RunStatus
from project_pilot.pipeline import Pipeline
from project_pilot.profile_loader import Profile, ProfileConstraints
from project_pilot.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = "https://www.freelancermap.de/projekte"
DETAIL1 = "https://www.freelancermap.de/projekt/senior-python-entwickler-backend-12345"
DETAIL2 = "https://www.freelancermap.de/projekt/data-engineer-azure-67890"
NOW = datetime(2026, 7, 21, 7, 20, tzinfo=UTC)  # ~8 min after card1's posted 07:12 UTC

LIST_HTML = """<!doctype html><html><body><ol class="project-list">
<li><article class="project-card"><h2 class="project-title">
<a href="/projekt/senior-python-entwickler-backend-12345">Senior Python</a>
</h2><div class="project-meta"><span class="project-location">Remote</span></div>
</article></li>
<li><article class="project-card"><h2 class="project-title">
<a href="/projekt/data-engineer-azure-67890">Data Engineer</a>
</h2><div class="project-meta"><span class="project-location">Muenchen</span></div>
</article></li>
</ol></body></html>"""


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


PAGES = {
    SEARCH: LIST_HTML,
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
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send_message(self, text: str, *, disable_preview: bool = True) -> bool:
        self.sent.append(text)
        return self.ok


def _settings() -> Settings:
    return Settings(search_urls=[SEARCH], match_threshold=60, analysis_window_min=30)


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
    client: _FakeClient,
    matcher: _FakeMatcher | None = None,
    telegram: _FakeNotifier | None = None,
    profile: Profile | None = None,
) -> Pipeline:
    return Pipeline(
        settings=_settings(),
        profile=profile or _profile(),
        session_factory=session_factory,
        client_factory=lambda: client,
        matcher=matcher or _FakeMatcher(),
        telegram=telegram,
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
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), telegram=notifier)
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.is_seed is True
    assert outcome.new == 2
    assert outcome.notified == 0
    assert notifier.sent == []
    async with session_factory() as db_session:
        listings = (await db_session.scalars(select(Listing))).all()
        assert len(listings) == 2
        assert all(listing.status is ListingStatus.SKIPPED_STALE for listing in listings)


async def test_full_run_notifies_match(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    notifier = _FakeNotifier(ok=True)
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), telegram=notifier)
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.is_seed is False
    assert outcome.new == 2
    assert outcome.evaluated == 2
    assert outcome.matched == 1
    assert outcome.notified == 1
    assert len(notifier.sent) == 1
    assert "Senior Python" in notifier.sent[0]
    async with session_factory() as db_session:
        repo = Repository(db_session)
        card1 = await repo.get_listing_by_hash(compute_url_hash(DETAIL1))
        card2 = await repo.get_listing_by_hash(compute_url_hash(DETAIL2))
        assert card1 is not None
        assert card1.notified_at is not None
        assert card2 is not None
        assert card2.notified_at is None


async def test_per_entry_isolation_skips_failing_detail(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pages = {SEARCH: LIST_HTML, DETAIL1: PAGES[DETAIL1]}  # DETAIL2 absent
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


async def test_notification_retry_on_next_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    failing = _FakeNotifier(ok=False)
    first = _pipeline(session_factory, client=_FakeClient(PAGES), telegram=failing)
    outcome1 = await first.run_once(now=NOW)
    assert outcome1.matched == 1
    assert outcome1.notified == 0
    assert len(failing.sent) == 1

    ok_notifier = _FakeNotifier(ok=True)
    second = _pipeline(session_factory, client=_FakeClient(PAGES), telegram=ok_notifier)
    outcome2 = await second.run_once(now=NOW + timedelta(minutes=15))
    assert outcome2.new == 0
    assert outcome2.notified == 1
    assert len(ok_notifier.sent) == 1


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
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), telegram=notifier)
    outcome = await pipeline.run_once(now=late)
    assert outcome.matched == 0
    assert outcome.notified == 0
    assert notifier.sent == []
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
        telegram=notifier,
        profile=_profile(blacklist=["fastapi"]),
    )
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.matched == 0
    assert notifier.sent == []


async def test_dry_run_without_telegram_does_not_notify(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_state(session_factory, watermark=NOW - timedelta(minutes=10))
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES), telegram=None)
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


async def test_source_blocked_sets_cooldown_and_warns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    warns = _FakeNotifier()
    pipeline = _pipeline(session_factory, client=_FakeClient(PAGES, block=True), telegram=warns)
    outcome = await pipeline.run_once(now=NOW)
    assert outcome.is_error is True
    assert any("Cooling down" in message for message in warns.sent)
    async with session_factory() as db_session:
        state = await Repository(db_session).get_source_state(SOURCE_NAME)
        assert state is not None
        assert state.cooldown_until is not None
        assert state.cooldown_until > NOW


async def test_cooldown_skips_next_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = _pipeline(
        session_factory, client=_FakeClient(PAGES, block=True), telegram=_FakeNotifier()
    )
    await first.run_once(now=NOW)

    second = _pipeline(session_factory, client=_FakeClient(PAGES), telegram=_FakeNotifier())
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
            telegram=warns,
        )
        outcome = await pipeline.run_once(now=NOW + timedelta(minutes=15 * index))
        assert outcome.is_error is True

    failure_warnings = [m for m in warns.sent if "consecutive failed runs" in m]
    assert len(failure_warnings) == 1

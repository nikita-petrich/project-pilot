"""Pipeline orchestration: dedupe, freshness, hard rules, LLM match, run protocol."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.config import SOURCE_NAME, Settings
from project_pilot.db import session_scope
from project_pilot.errors import SourceBlockedError, SourceUnavailableError
from project_pilot.evaluation.freshness import evaluate_freshness
from project_pilot.evaluation.llm import (
    LlmEvaluation,
    LlmProbe,
    is_match_notifiable,
    probe_llm,
    render_listing,
)
from project_pilot.evaluation.rules import apply_hard_rules
from project_pilot.health import LLM_COMPONENT, HealthAlerter, HealthIssue
from project_pilot.ingestion.client import BASE_URL
from project_pilot.ingestion.normalize import next_page_url
from project_pilot.ingestion.parser import (
    ListingSummary,
    ParsedListing,
    parse_detail_page,
    parse_list_page,
)
from project_pilot.ingestion.watermark import evaluate_page
from project_pilot.models import (
    Evaluation,
    EvaluationStage,
    Listing,
    ListingStatus,
    RunStatus,
    SourceState,
    Verdict,
)
from project_pilot.notification.messages import MatchMessage, from_stored
from project_pilot.profile_loader import Profile
from project_pilot.repository import Repository

logger = logging.getLogger(__name__)

# A normal run stops on page 1 (the newest listing is already known or older than
# the watermark). This deep cap only engages after an outage, when many pages of
# genuinely new listings have accumulated, so the lossless-DB guarantee is kept
# instead of being silently truncated at 2 pages. Politeness delays bound the rate;
# exhausting even this cap is surfaced (never silent) and holds the watermark.
MAX_LIST_PAGES = 25
COOLDOWN_HOURS = 6
FAILURE_WARNING_THRESHOLD = 3
# Only matches first seen within this window are (re)sent. It comfortably covers
# retrying a failed send across an outage, while stopping a lowered MATCH_THRESHOLD
# from retro-flooding the channel with every historical below-threshold listing.
NOTIFY_MAX_AGE = timedelta(days=2)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HttpResponse(Protocol):
    @property
    def text(self) -> str: ...


class SourceClient(Protocol):
    async def check_robots(self, urls: list[str]) -> None: ...
    async def get(self, url: str) -> HttpResponse: ...
    async def aclose(self) -> None: ...


class Matcher(Protocol):
    async def evaluate(self, *, profile_text: str, listing_text: str) -> LlmEvaluation: ...


class MatchNotifier(Protocol):
    """The notification channel: one topic per match with its card inside, and
    operator warnings over the same channel without a topic."""

    async def create_topic(self, message: MatchMessage) -> int | None: ...
    async def notify(self, message: MatchMessage, thread_id: int | None = None) -> bool: ...
    async def notify_warning(self, text: str) -> bool: ...


type ClientFactory = Callable[[], SourceClient]


@dataclass(slots=True)
class RunOutcome:
    status: RunStatus
    fetched: int = 0
    new: int = 0
    evaluated: int = 0
    matched: int = 0
    notified: int = 0
    errors: int = 0
    error: str | None = None
    is_seed: bool = False
    pagination_truncated: bool = False
    # Stage 3 health. An llm_error is a *stored verdict*, not a run failure, so it
    # never shows up in `errors` — these two are what make it visible.
    llm_ok: int = 0
    llm_errors: int = 0
    llm_issue: HealthIssue | None = None

    @property
    def is_error(self) -> bool:
        return self.status is RunStatus.ERROR


class Pipeline:
    """Orchestrates one scan: fetch, dedupe, evaluate fresh entries, notify matches."""

    def __init__(
        self,
        *,
        settings: Settings,
        profile: Profile,
        session_factory: async_sessionmaker[AsyncSession],
        client_factory: ClientFactory,
        matcher: Matcher,
        base_url: str = BASE_URL,
        llm_probe: LlmProbe | None = None,
        alerter: HealthAlerter | None = None,
        notifier: MatchNotifier | None = None,
    ) -> None:
        self._settings = settings
        self._profile = profile
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._matcher = matcher
        self._base_url = base_url
        self._source = SOURCE_NAME
        self._llm_probe = llm_probe
        self._alerter = alerter or HealthAlerter(self._send_operator_message)
        self._notifier = notifier

    async def run_once(self, now: datetime | None = None) -> RunOutcome:
        """One scan in three phases: scan/evaluate (one unit of work), notify, record.

        Notification runs only after the main unit of work has committed, so a
        opened match thread can never be un-marked by a failed run commit; the
        run row is finalized in its own short session for the same reason.
        """
        now = now or _utcnow()
        search_urls = self._settings.require_search_urls()
        outcome = RunOutcome(status=RunStatus.SUCCESS)
        run_id: int | None = None
        blocked = False
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            state = await repo.get_or_create_source_state(self._source)
            if state.cooldown_until is not None and state.cooldown_until > now:
                logger.info("source in cooldown until %s; skipping run", state.cooldown_until)
                return RunOutcome(status=RunStatus.SUCCESS, error="skipped: in cooldown")

            run_id = (await repo.start_run()).id
            try:
                await self._execute(repo, search_urls, now, outcome, state.watermark_at)
            except SourceBlockedError as err:
                blocked = True
                outcome.status = RunStatus.ERROR
                outcome.error = f"source blocked: {err}"
                logger.warning("run aborted: %s", outcome.error)
            except SourceUnavailableError as err:
                # Expected on a home connection: no traceback, the watermark stays
                # put and the next run closes the gap.
                outcome.status = RunStatus.ERROR
                outcome.error = f"source unreachable: {err}"
                logger.warning("run aborted: %s; retrying next run", outcome.error)
            except Exception as err:
                outcome.status = RunStatus.ERROR
                outcome.error = f"run failed: {err}"
                logger.exception("run failed")
            else:
                outcome.status = RunStatus.PARTIAL if outcome.errors else RunStatus.SUCCESS

            await self._finalize_state(state, outcome, now, blocked=blocked)

        await self._report_llm_health(outcome, now)
        if not outcome.is_seed and not outcome.is_error:
            await self._notify(now, outcome)
        await self._record_run(run_id, outcome, now)
        return outcome

    async def check_llm(self, *, now: datetime | None = None) -> HealthIssue | None:
        """Preflight the LLM and alert on the result; returns the issue, never raises."""
        if self._llm_probe is None:
            return None
        issue = await probe_llm(self._llm_probe, model=self._settings.llm_model)
        await self.report_llm_issue(issue, now=now)
        return issue

    async def report_llm_issue(
        self, issue: HealthIssue | None, *, now: datetime | None = None
    ) -> None:
        """Publish stage 3's health: alert on a problem, announce recovery, else stay quiet."""
        if issue is not None:
            await self._alerter.failed(issue, now=now or _utcnow())
        else:
            await self._alerter.recovered(LLM_COMPONENT)

    async def _report_llm_health(self, outcome: RunOutcome, now: datetime) -> None:
        """A run that only produced llm_errors looks successful — say so out loud.

        Stays silent when the run reached stage 3 for nothing at all (no fresh
        listings): no evidence either way is not evidence of recovery.
        """
        if outcome.llm_issue is not None:
            await self.report_llm_issue(outcome.llm_issue, now=now)
        elif outcome.llm_ok:
            await self.report_llm_issue(None, now=now)

    async def _record_run(self, run_id: int | None, outcome: RunOutcome, now: datetime) -> None:
        if run_id is None:  # cooldown skip: no run was started
            return
        async with session_scope(self._session_factory) as session:
            await Repository(session).record_run_outcome(
                run_id,
                started_at=now,
                status=outcome.status,
                fetched=outcome.fetched,
                new=outcome.new,
                evaluated=outcome.evaluated,
                matched=outcome.matched,
                notified=outcome.notified,
                error=outcome.error,
            )

    async def _finalize_state(
        self, state: SourceState, outcome: RunOutcome, now: datetime, *, blocked: bool
    ) -> None:
        if outcome.status is RunStatus.ERROR:
            state.consecutive_failures += 1
            if blocked:
                state.cooldown_until = now + timedelta(hours=COOLDOWN_HOURS)
                await self._warn(
                    "project-pilot: source blocked (403/captcha). Cooling down until "
                    f"{state.cooldown_until:%Y-%m-%d %H:%M} UTC."
                )
            if state.consecutive_failures == FAILURE_WARNING_THRESHOLD:
                await self._warn(
                    f"project-pilot: {FAILURE_WARNING_THRESHOLD} consecutive failed runs. "
                    f"Last error: {outcome.error}"
                )
        else:
            state.consecutive_failures = 0
            state.cooldown_until = None

    async def _warn(self, text: str) -> None:
        await self._send_operator_message(f"⚠️ {text}")

    async def _send_operator_message(self, text: str) -> None:
        """Deliver an operator message verbatim (the caller owns the wording and icon)."""
        if self._notifier is not None:
            await self._notifier.notify_warning(text)
        else:
            logger.warning("operator message (no notifier configured): %s", text)

    async def _execute(
        self,
        repo: Repository,
        search_urls: list[str],
        now: datetime,
        outcome: RunOutcome,
        watermark: datetime | None,
    ) -> None:
        outcome.is_seed = await repo.count_listings() == 0

        client = self._client_factory()
        try:
            await client.check_robots([*search_urls, self._base_url])
            summaries = await self._collect_new_summaries(
                client, repo, search_urls, watermark, outcome
            )
            created = await self._fetch_and_store(client, repo, summaries, now, outcome)
        finally:
            await client.aclose()

        if outcome.is_seed:
            await self._finish_seed(repo, created)
        else:
            await self._evaluate(repo, created, watermark, now, outcome)

        if outcome.errors:
            # A skipped listing was never stored (or evaluated); advancing the
            # watermark would drop it from the next run's pagination forever
            # (lossless-DB rule). The known-hash stop keeps the re-scan cheap.
            logger.warning(
                "%d per-listing error(s); watermark held so the next run re-collects",
                outcome.errors,
            )
        elif outcome.pagination_truncated:
            # We stopped because of the page cap, not because we reached already-known
            # listings, so listings may lie beyond the last page fetched. Hold the
            # watermark rather than claim we caught up, and make the gap visible.
            logger.warning(
                "list pagination hit the %d-page cap before reaching known listings; "
                "watermark held so the next run re-collects the backlog",
                MAX_LIST_PAGES,
            )
        else:
            await repo.set_watermark(self._source, now)

    async def _collect_new_summaries(
        self,
        client: SourceClient,
        repo: Repository,
        search_urls: list[str],
        watermark: datetime | None,
        outcome: RunOutcome,
    ) -> list[ListingSummary]:
        collected: dict[str, ListingSummary] = {}
        for search_url in search_urls:
            page_url: str | None = search_url
            for _ in range(MAX_LIST_PAGES):
                if page_url is None:
                    break
                response = await client.get(page_url)
                summaries = parse_list_page(response.text, self._base_url)
                if not summaries:
                    break  # empty results page: end of pagination
                outcome.fetched += len(summaries)
                known = await repo.get_known_hashes([summary.url_hash for summary in summaries])
                decision = evaluate_page(summaries, known, watermark)
                for summary in decision.new_summaries:
                    collected.setdefault(summary.url_hash, summary)
                if decision.should_stop:
                    break
                page_url = next_page_url(page_url)
            else:
                # The loop ran the full cap without an early break, i.e. without ever
                # reaching a known/older listing: the newest-first pages are truncated.
                outcome.pagination_truncated = True
        return list(collected.values())

    async def _fetch_and_store(
        self,
        client: SourceClient,
        repo: Repository,
        summaries: list[ListingSummary],
        now: datetime,
        outcome: RunOutcome,
    ) -> list[tuple[Listing, ParsedListing]]:
        created: list[tuple[Listing, ParsedListing]] = []
        for summary in summaries:
            try:
                response = await client.get(summary.external_url)
                parsed = parse_detail_page(
                    response.text,
                    self._base_url,
                    source=self._source,
                    external_url=summary.external_url,
                )
                # Savepoint: a DB failure on this one listing must not poison the
                # run-wide session for every listing after it.
                async with repo.savepoint():
                    listing, was_created = await repo.upsert_listing(
                        _to_listing(parsed, summary, now)
                    )
                if was_created:
                    outcome.new += 1
                    created.append((listing, parsed))
            except (SourceBlockedError, SourceUnavailableError):
                # A dead connection fails every remaining fetch too: abort the run
                # on the clean path instead of counting each as a listing error.
                raise
            except Exception as err:
                outcome.errors += 1
                logger.warning("skipping listing %s: %s", summary.external_url, err)
        return created

    async def _finish_seed(
        self, repo: Repository, created: list[tuple[Listing, ParsedListing]]
    ) -> None:
        for listing, _parsed in created:
            listing.status = ListingStatus.SKIPPED_STALE
            await repo.add_evaluation(
                Evaluation(
                    listing_id=listing.id,
                    stage=EvaluationStage.FRESHNESS,
                    verdict=Verdict.SKIPPED_STALE,
                    reason={"reason": "seed run: persisted without analysis"},
                )
            )

    async def _evaluate(
        self,
        repo: Repository,
        created: list[tuple[Listing, ParsedListing]],
        watermark: datetime | None,
        now: datetime,
        outcome: RunOutcome,
    ) -> None:
        window = self._settings.analysis_window_min
        for listing, parsed in created:
            try:
                # Same savepoint rationale as in _fetch_and_store: contain one
                # listing's DB failure instead of poisoning the shared session.
                async with repo.savepoint():
                    evaluated, matched = await self._evaluate_one(
                        repo,
                        listing,
                        parsed,
                        watermark=watermark,
                        now=now,
                        window=window,
                        outcome=outcome,
                    )
                outcome.evaluated += evaluated
                outcome.matched += matched
            except Exception as err:
                outcome.errors += 1
                logger.warning("evaluation failed for %s: %s", listing.external_url, err)

    async def _evaluate_one(
        self,
        repo: Repository,
        listing: Listing,
        parsed: ParsedListing,
        *,
        watermark: datetime | None,
        now: datetime,
        window: int,
        outcome: RunOutcome,
    ) -> tuple[int, int]:
        """Run stages 1-3 for one listing; returns the (evaluated, matched) deltas."""
        fresh = evaluate_freshness(
            posted_at=listing.posted_at,
            posted_at_precision=listing.posted_at_precision,
            watermark=watermark,
            now=now,
            window_minutes=window,
        )
        if not fresh.is_fresh:
            listing.status = ListingStatus.SKIPPED_STALE
            await repo.add_evaluation(
                Evaluation(
                    listing_id=listing.id,
                    stage=EvaluationStage.FRESHNESS,
                    verdict=Verdict.SKIPPED_STALE,
                    reason=fresh.reason,
                )
            )
            return 0, 0

        rule = apply_hard_rules(f"{listing.title}\n{parsed.description}", self._profile.constraints)
        if not rule.passed:
            listing.status = ListingStatus.EVALUATED
            await repo.add_evaluation(
                Evaluation(
                    listing_id=listing.id,
                    stage=EvaluationStage.HARD_RULE,
                    verdict=Verdict.NO_MATCH,
                    reason=rule.reason,
                    profile_hash=self._profile.profile_hash,
                )
            )
            return 1, 0

        llm = await self._matcher.evaluate(
            profile_text=self._profile.text, listing_text=render_listing(parsed)
        )
        if llm.is_error:
            outcome.llm_errors += 1
            # The last cause wins: within one run they are the same failure anyway.
            outcome.llm_issue = llm.issue
        else:
            outcome.llm_ok += 1
        listing.status = ListingStatus.EVALUATED
        await repo.add_evaluation(
            Evaluation(
                listing_id=listing.id,
                stage=EvaluationStage.LLM,
                verdict=Verdict.MATCH if llm.is_match else Verdict.NO_MATCH,
                score=llm.score,
                reason=llm.reason(),
                model=llm.model,
                prompt_version=llm.prompt_version,
                profile_hash=self._profile.profile_hash,
                tokens_in=llm.tokens_in,
                tokens_out=llm.tokens_out,
                latency_ms=llm.latency_ms,
            )
        )
        is_matched = is_match_notifiable(llm, self._settings.match_threshold)
        return 1, 1 if is_matched else 0

    async def _notify(self, now: datetime, outcome: RunOutcome) -> None:
        """Open one topic per pending match and send its card, durable per match.

        Runs in its own session after the scan's unit of work has committed, and
        commits after every successful send, so a delivered notification can
        never be rolled back into "unnotified" and sent twice. A failed send
        leaves the listing pending and it is retried on the next run.

        The topic mapping is committed as soon as the topic exists, *before* the
        send: creating a topic is an external side effect that no rollback
        undoes, so a mapping lost to a failed send would have the retry open a
        second topic for the same project.
        """
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            pending = await repo.get_unnotified_matches(
                min_score=self._settings.match_threshold, not_before=now - NOTIFY_MAX_AGE
            )
            if not pending:
                return
            if self._notifier is None:
                logger.info(
                    "dry-run: %d match(es) not pushed (no notifier configured)", len(pending)
                )
                return
            notifier = self._notifier  # narrowed above; the helper needs it non-optional
            failed = 0
            for listing in pending:  # one push per match, marked only on success
                message = from_stored(listing, now)
                if message.onsite_only:
                    # Mark it handled so it leaves the pending set after one skip
                    # instead of being re-loaded and re-skipped on every run.
                    await repo.mark_notified([listing], now)
                    logger.info(
                        "suppressing on-site-only match (marked handled): %s",
                        listing.external_url,
                    )
                    continue
                thread_id = await self._thread_for(repo, session, notifier, listing, message)
                if await notifier.notify(message, thread_id):
                    await repo.mark_notified([listing], now)
                    await session.commit()
                    outcome.notified += 1
                    logger.info("match sent: %s (topic %s)", listing.external_url, thread_id)
                else:
                    failed += 1
            if failed:
                logger.warning("notification failed; %d match(es) will retry next run", failed)

    async def _thread_for(
        self,
        repo: Repository,
        session: AsyncSession,
        notifier: MatchNotifier,
        listing: Listing,
        message: MatchMessage,
    ) -> int | None:
        """This listing's topic, opening one on first sight; None if impossible.

        Committed right here, because the topic already exists in Telegram by
        then. None means the chat cannot host topics (not a forum, or the bot is
        not an admin) — the card then goes to the group's general area rather
        than nowhere.
        """
        existing = await repo.get_thread(listing.id)
        if existing is not None:
            return existing.thread_id
        thread_id = await notifier.create_topic(message)
        if thread_id is None:
            logger.warning(
                "no topic for %s; sending to the group's general area", listing.external_url
            )
            return None
        await repo.record_thread(listing.id, thread_id)
        await session.commit()
        return thread_id


def _to_listing(parsed: ParsedListing, summary: ListingSummary, now: datetime) -> Listing:
    posted_at = parsed.posted_at or summary.posted_at
    precision = (
        parsed.posted_at_precision if parsed.posted_at is not None else summary.posted_at_precision
    )
    return Listing(
        source=parsed.source,
        external_url=parsed.external_url,
        url_hash=parsed.url_hash,
        # Scraped display values are truncated to their column limits: an oversized
        # title must not fail the INSERT (the full text survives in `raw`).
        title=parsed.title[:512],
        description=parsed.description,
        skills=parsed.skills,
        start_date=parsed.start_date,
        start_asap=parsed.start_asap,
        end_date=parsed.end_date,
        location=parsed.location[:256] if parsed.location else None,
        remote_status=parsed.remote_status,
        posted_at=posted_at,
        posted_at_precision=precision,
        first_seen_at=now,
        last_seen_at=now,
        status=ListingStatus.NEW,
        raw=parsed.raw,
    )

"""Pipeline orchestration: dedupe, freshness, hard rules, LLM match, run protocol."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.config import SOURCE_NAME, Settings
from project_pilot.db import session_scope
from project_pilot.errors import SourceBlockedError
from project_pilot.evaluation.freshness import evaluate_freshness
from project_pilot.evaluation.llm import LlmEvaluation, is_match_notifiable, render_listing
from project_pilot.evaluation.rules import apply_hard_rules
from project_pilot.ingestion.client import BASE_URL
from project_pilot.ingestion.parser import (
    ListingSummary,
    ParsedListing,
    parse_detail_page,
    parse_list_page,
    parse_next_page_url,
)
from project_pilot.ingestion.watermark import evaluate_page
from project_pilot.models import (
    Evaluation,
    EvaluationStage,
    Listing,
    ListingStatus,
    RunStatus,
    Verdict,
)
from project_pilot.notification.telegram import MatchMessage, build_digest
from project_pilot.profile_loader import Profile
from project_pilot.repository import Repository

logger = logging.getLogger(__name__)

MAX_LIST_PAGES = 2


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


class Notifier(Protocol):
    async def send_message(self, text: str, *, disable_preview: bool = True) -> bool: ...


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
        telegram: Notifier | None,
        base_url: str = BASE_URL,
    ) -> None:
        self._settings = settings
        self._profile = profile
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._matcher = matcher
        self._telegram = telegram
        self._base_url = base_url
        self._source = SOURCE_NAME

    async def run_once(self, now: datetime | None = None) -> RunOutcome:
        now = now or _utcnow()
        search_urls = self._settings.require_search_urls()
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            run = await repo.start_run()
            outcome = RunOutcome(status=RunStatus.SUCCESS)
            try:
                await self._execute(repo, search_urls, now, outcome)
            except SourceBlockedError as err:
                outcome.status = RunStatus.ERROR
                outcome.error = f"source blocked: {err}"
                logger.warning("run aborted: %s", outcome.error)
            except Exception as err:
                outcome.status = RunStatus.ERROR
                outcome.error = f"run failed: {err}"
                logger.exception("run failed")
            else:
                outcome.status = RunStatus.PARTIAL if outcome.errors else RunStatus.SUCCESS
            await repo.finalize_run(
                run,
                status=outcome.status,
                fetched=outcome.fetched,
                new=outcome.new,
                evaluated=outcome.evaluated,
                matched=outcome.matched,
                notified=outcome.notified,
                error=outcome.error,
            )
        return outcome

    async def _execute(
        self, repo: Repository, search_urls: list[str], now: datetime, outcome: RunOutcome
    ) -> None:
        state = await repo.get_source_state(self._source)
        watermark = state.watermark_at if state is not None else None
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
            await self._notify(repo, now, outcome)

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
                html = response.text
                summaries = parse_list_page(html, self._base_url)
                outcome.fetched += len(summaries)
                known = await repo.get_known_hashes([summary.url_hash for summary in summaries])
                decision = evaluate_page(summaries, known, watermark)
                for summary in decision.new_summaries:
                    collected.setdefault(summary.url_hash, summary)
                if decision.should_stop:
                    break
                page_url = parse_next_page_url(html, self._base_url)
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
                listing, was_created = await repo.upsert_listing(_to_listing(parsed, summary, now))
                if was_created:
                    outcome.new += 1
                    created.append((listing, parsed))
            except SourceBlockedError:
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
        threshold = self._settings.match_threshold
        for listing, parsed in created:
            try:
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
                    continue

                rule = apply_hard_rules(
                    f"{listing.title}\n{parsed.description}", self._profile.constraints
                )
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
                    outcome.evaluated += 1
                    continue

                llm = await self._matcher.evaluate(
                    profile_text=self._profile.text, listing_text=render_listing(parsed)
                )
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
                outcome.evaluated += 1
                if is_match_notifiable(llm, threshold):
                    outcome.matched += 1
            except Exception as err:
                outcome.errors += 1
                logger.warning("evaluation failed for %s: %s", listing.external_url, err)

    async def _notify(self, repo: Repository, now: datetime, outcome: RunOutcome) -> None:
        pending = await repo.get_unnotified_matches(min_score=self._settings.match_threshold)
        if not pending:
            return
        messages = [_to_match_message(listing) for listing in pending]
        if self._telegram is None:
            logger.info("dry-run: %d match(es) not sent (no Telegram configured)", len(messages))
            return
        sent = await self._telegram.send_message(build_digest(messages))
        if sent:
            await repo.mark_notified(pending, now)
            outcome.notified = len(pending)
        else:
            logger.warning("telegram send failed; %d match(es) will retry next run", len(pending))


def _to_listing(parsed: ParsedListing, summary: ListingSummary, now: datetime) -> Listing:
    posted_at = parsed.posted_at or summary.posted_at
    precision = (
        parsed.posted_at_precision if parsed.posted_at is not None else summary.posted_at_precision
    )
    return Listing(
        source=parsed.source,
        external_url=parsed.external_url,
        url_hash=parsed.url_hash,
        title=parsed.title,
        description=parsed.description,
        skills=parsed.skills,
        start_date=parsed.start_date,
        start_asap=parsed.start_asap,
        end_date=parsed.end_date,
        location=parsed.location,
        remote_status=parsed.remote_status,
        posted_at=posted_at,
        posted_at_precision=precision,
        first_seen_at=now,
        last_seen_at=now,
        status=ListingStatus.NEW,
        raw=parsed.raw,
    )


def _latest_match_evaluation(listing: Listing) -> Evaluation | None:
    matches = [
        evaluation
        for evaluation in listing.evaluations
        if evaluation.stage is EvaluationStage.LLM and evaluation.verdict is Verdict.MATCH
    ]
    if not matches:
        return None
    return max(matches, key=lambda evaluation: evaluation.created_at)


def _reasons_from(evaluation: Evaluation | None) -> list[str]:
    if evaluation is None:
        return []
    raw = evaluation.reason.get("reasons")
    if isinstance(raw, list):
        return [str(reason) for reason in raw]
    return []


def _to_match_message(listing: Listing) -> MatchMessage:
    evaluation = _latest_match_evaluation(listing)
    score = evaluation.score if evaluation is not None and evaluation.score is not None else 0
    if listing.start_asap:
        start: str | None = "ab sofort"
    elif listing.start_date is not None:
        start = listing.start_date.isoformat()
    else:
        start = None
    return MatchMessage(
        title=listing.title,
        url=listing.external_url,
        score=score,
        reasons=_reasons_from(evaluation),
        start=start,
        location=listing.location,
        remote=listing.remote_status.value,
    )

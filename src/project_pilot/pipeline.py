"""Pipeline orchestration: dedupe, freshness, hard rules, LLM match, run protocol."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.config import SOURCE_NAME, Settings
from project_pilot.db import session_scope
from project_pilot.errors import SourceBlockedError
from project_pilot.evaluation.freshness import evaluate_freshness
from project_pilot.evaluation.llm import LlmEvaluation, is_match_notifiable, render_listing
from project_pilot.evaluation.rules import apply_hard_rules
from project_pilot.ingestion.client import BASE_URL
from project_pilot.ingestion.normalize import (
    detect_language,
    extract_contact_person,
    is_onsite_only,
    looks_like_company,
    next_page_url,
)
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
from project_pilot.notification.telegram import MatchMessage, format_match
from project_pilot.profile_loader import Profile
from project_pilot.repository import Repository

logger = logging.getLogger(__name__)

MAX_LIST_PAGES = 2
COOLDOWN_HOURS = 6
FAILURE_WARNING_THRESHOLD = 3


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
            state = await repo.get_or_create_source_state(self._source)
            if state.cooldown_until is not None and state.cooldown_until > now:
                logger.info("source in cooldown until %s; skipping run", state.cooldown_until)
                return RunOutcome(status=RunStatus.SUCCESS, error="skipped: in cooldown")

            run = await repo.start_run()
            outcome = RunOutcome(status=RunStatus.SUCCESS)
            blocked = False
            try:
                await self._execute(repo, search_urls, now, outcome, state.watermark_at)
            except SourceBlockedError as err:
                blocked = True
                outcome.status = RunStatus.ERROR
                outcome.error = f"source blocked: {err}"
                logger.warning("run aborted: %s", outcome.error)
            except Exception as err:
                outcome.status = RunStatus.ERROR
                outcome.error = f"run failed: {err}"
                logger.exception("run failed")
            else:
                outcome.status = RunStatus.PARTIAL if outcome.errors else RunStatus.SUCCESS

            await self._finalize_state(state, outcome, now, blocked=blocked)
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
        if self._telegram is not None:
            await self._telegram.send_message(f"⚠️ {text}")
        else:
            logger.warning("warning (no telegram): %s", text)

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
        if self._telegram is None:
            logger.info("dry-run: %d match(es) not sent (no Telegram configured)", len(pending))
            return
        for listing in pending:  # one message per match, marked notified only on a successful send
            message = _to_match_message(listing, now)
            if message.onsite_only:
                logger.info("skipping on-site-only match: %s", listing.external_url)
                continue
            if await self._telegram.send_message(format_match(message)):
                await repo.mark_notified([listing], now)
                outcome.notified += 1
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


_CONTRACT_LABELS = {
    "contracting": "Freiberuflich",
    "contractor": "Freiberuflich",
    "freelance": "Freiberuflich",
    "employee_leasing": "Arbeitnehmerüberlassung",
    "permanent_position": "Festanstellung",
    "temporary_employment": "Zeitarbeit",
}
_LANGUAGE_LABELS = {"de": "Deutsch", "en": "Englisch"}


class _RawContract(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    contract_type: str | None = Field(default=None, alias="contractType")
    remote_in_percent: int | None = Field(default=None, alias="remoteInPercent")


class _RawIndustry(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name_de: str | None = Field(default=None, alias="nameDe")


class _RawFields(BaseModel):
    """The subset of the stored raw source record used to enrich a match message."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    company: str | None = None
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    workload: int | None = None
    duration_in_months: int | None = Field(default=None, alias="durationInMonths")
    duration_text: str | None = Field(default=None, alias="durationText")
    expires: str | None = None
    extension_possible: bool | None = Field(default=None, alias="extensionPossible")
    is_endcustomer_project: bool | None = Field(default=None, alias="isEndcustomerProject")
    industry: _RawIndustry | None = None
    contract: _RawContract | None = Field(default=None, alias="contractType")


def _eval_list(evaluation: Evaluation | None, key: str) -> list[str]:
    if evaluation is None:
        return []
    value = evaluation.reason.get(key)
    return [str(item) for item in value] if isinstance(value, list) else []


def _relative_de(posted_at: datetime, now: datetime) -> str | None:
    minutes = int((now - posted_at).total_seconds() // 60)
    if minutes < 0:
        return None
    if minutes < 60:
        return f"vor {minutes} Min"
    if minutes < 1440:
        return f"vor {minutes // 60} Std"
    return f"vor {minutes // 1440} Tg"


def _expires_label(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return None


def _to_match_message(listing: Listing, now: datetime) -> MatchMessage:
    evaluation = _latest_match_evaluation(listing)
    score = evaluation.score if evaluation is not None and evaluation.score is not None else 0
    raw = _RawFields.model_validate(listing.raw or {})

    if listing.start_asap:
        start: str | None = "ab sofort"
    elif listing.start_date is not None:
        start = listing.start_date.strftime("%d.%m.%Y")
    else:
        start = None

    # remoteInPercent == 0 is freelancermap's "not specified" default (often wrong for
    # agency posts), so only show it when it carries a real signal; the location line
    # still conveys remote/onsite. Genuine hybrids (1..99) show the on-site share.
    remote_pct = raw.contract.remote_in_percent if raw.contract else None
    if remote_pct is None or remote_pct <= 0:
        remote_label: str | None = None
    elif remote_pct >= 100:
        remote_label = "100%"
    else:
        remote_label = f"{remote_pct}% ({100 - remote_pct}% vor Ort)"

    contract_type = None
    if raw.contract and raw.contract.contract_type:
        contract_type = _CONTRACT_LABELS.get(raw.contract.contract_type, raw.contract.contract_type)

    duration_label = raw.duration_text or (
        f"{raw.duration_in_months} Mon" if raw.duration_in_months else None
    )
    if duration_label and raw.extension_possible:
        duration_label += " (+ Verlängerung)"

    # The structured contact is the real person for direct posts, but the agency name for
    # brokered ones; in that case pull the person out of the description text instead.
    structured = " ".join(part for part in (raw.first_name, raw.last_name) if part) or None
    if structured and not looks_like_company(structured) and structured != raw.company:
        contact_name: str | None = structured
    else:
        contact_name = extract_contact_person(listing.description or "")

    language = detect_language(listing.description or listing.title)

    return MatchMessage(
        title=listing.title,
        url=listing.external_url,
        score=score,
        company=raw.company,
        contact_name=contact_name,
        is_endcustomer=raw.is_endcustomer_project,
        location=listing.location,
        remote_label=remote_label,
        contract_type=contract_type,
        workload_label=f"{raw.workload}%" if raw.workload else None,
        duration_label=duration_label,
        start=start,
        posted_ago=_relative_de(listing.posted_at, now) if listing.posted_at else None,
        expires_label=_expires_label(raw.expires),
        industry=raw.industry.name_de if raw.industry else None,
        language=_LANGUAGE_LABELS.get(language) if language else None,
        skills=list(listing.skills or []),
        reasons=_eval_list(evaluation, "reasons"),
        matching_skills=_eval_list(evaluation, "matching_skills"),
        missing_requirements=_eval_list(evaluation, "missing_requirements"),
        risk_flags=_eval_list(evaluation, "risk_flags"),
        description=listing.description or "",
        onsite_only=is_onsite_only(remote_pct, listing.location, listing.description or ""),
    )

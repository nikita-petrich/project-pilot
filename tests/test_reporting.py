"""Tests for the reporting service."""

from sqlalchemy.ext.asyncio import AsyncSession

from project_pilot.models import (
    Evaluation,
    EvaluationStage,
    Listing,
    ListingStatus,
    Verdict,
)
from project_pilot.reporting import ReportingService, format_report
from project_pilot.repository import Repository


async def _fixture_data(session: AsyncSession) -> None:
    repo = Repository(session)

    matched, _ = await repo.upsert_listing(
        Listing(
            source="freelancermap",
            external_url="https://x/1",
            url_hash="h1",
            title="Match",
            status=ListingStatus.EVALUATED,
        )
    )
    await repo.add_evaluation(
        Evaluation(
            listing_id=matched.id,
            stage=EvaluationStage.LLM,
            verdict=Verdict.MATCH,
            score=80,
            reason={"reasons": ["good fit"]},
            model="gpt",
            prompt_version="match.v1",
            tokens_in=100,
            tokens_out=20,
        )
    )

    rejected, _ = await repo.upsert_listing(
        Listing(
            source="freelancermap",
            external_url="https://x/2",
            url_hash="h2",
            title="No",
            status=ListingStatus.EVALUATED,
        )
    )
    await repo.add_evaluation(
        Evaluation(
            listing_id=rejected.id,
            stage=EvaluationStage.HARD_RULE,
            verdict=Verdict.NO_MATCH,
            reason={"rule": "blacklist", "matched_term": "wordpress"},
        )
    )

    stale, _ = await repo.upsert_listing(
        Listing(
            source="freelancermap",
            external_url="https://x/3",
            url_hash="h3",
            title="Stale",
            status=ListingStatus.SKIPPED_STALE,
        )
    )
    await repo.add_evaluation(
        Evaluation(
            listing_id=stale.id,
            stage=EvaluationStage.FRESHNESS,
            verdict=Verdict.SKIPPED_STALE,
            reason={"reason": "stale"},
        )
    )
    await session.commit()


async def test_verdict_distribution(session: AsyncSession) -> None:
    await _fixture_data(session)
    assert await ReportingService(session).verdict_distribution() == {
        "match": 1,
        "no_match": 1,
        "skipped_stale": 1,
    }


async def test_listings_by_status(session: AsyncSession) -> None:
    await _fixture_data(session)
    by_status = await ReportingService(session).listings_by_status()
    assert by_status["evaluated"] == 2
    assert by_status["skipped_stale"] == 1


async def test_matches_per_day(session: AsyncSession) -> None:
    await _fixture_data(session)
    per_day = await ReportingService(session).matches_per_day(7)
    assert sum(count for _, count in per_day) == 1


async def test_top_no_match_terms(session: AsyncSession) -> None:
    await _fixture_data(session)
    assert ("wordpress", 1) in await ReportingService(session).top_no_match_terms(10)


async def test_token_usage(session: AsyncSession) -> None:
    await _fixture_data(session)
    usage = await ReportingService(session).token_usage(7)
    assert usage.llm_calls == 1
    assert usage.tokens_in == 100
    assert usage.tokens_out == 20


async def test_build_and_format_report(session: AsyncSession) -> None:
    await _fixture_data(session)
    report = await ReportingService(session).build_report()
    assert report.total_listings == 3
    text = format_report(report)
    assert "project-pilot stats" in text
    assert "wordpress" in text

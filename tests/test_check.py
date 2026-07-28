"""Tests for the manual /check evaluation service (fake matcher; one DB-backed case)."""

from typing import Literal, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.db import session_scope
from project_pilot.errors import ApplicationStateError
from project_pilot.evaluation.check import CheckService
from project_pilot.evaluation.llm import LlmEvaluation
from project_pilot.evaluation.schemas import MatchVerdict
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import EvaluationStage, Listing, PostedPrecision, RemoteStatus, Verdict
from project_pilot.profile_loader import Profile, ProfileConstraints
from project_pilot.repository import Repository

THRESHOLD = 60


def _llm(
    verdict: Literal["match", "no_match"] = "match", score: int = 80, *, is_error: bool = False
) -> LlmEvaluation:
    return LlmEvaluation(
        verdict=MatchVerdict(
            verdict=verdict,
            score=score,
            reasons=["Passt zum Profil"],
            matching_skills=["python"],
            missing_requirements=["kubernetes"],
            risk_flags=[],
        ),
        model="test-model",
        prompt_version="match.v1",
        tokens_in=10,
        tokens_out=5,
        latency_ms=1,
        is_error=is_error,
    )


class _FakeMatcher:
    def __init__(self, evaluation: LlmEvaluation) -> None:
        self.evaluation = evaluation
        self.listing_texts: list[str] = []

    async def evaluate(self, *, profile_text: str, listing_text: str) -> LlmEvaluation:
        self.listing_texts.append(listing_text)
        return self.evaluation


def _profile(blacklist: list[str] | None = None) -> Profile:
    return Profile(
        text="Senior Python engineer",
        constraints=ProfileConstraints(blacklist=blacklist or [], must_have=[]),
        profile_hash="hash",
    )


def _service(
    matcher: _FakeMatcher,
    *,
    blacklist: list[str] | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> CheckService:
    return CheckService(
        session_factory=session_factory
        or cast("async_sessionmaker[AsyncSession]", None),  # DB-free checks never touch it
        matcher=matcher,
        profile=_profile(blacklist),
        threshold=THRESHOLD,
    )


def _parsed() -> ParsedListing:
    return ParsedListing(
        source="freelancermap",
        external_url="https://www.freelancermap.de/projekt/ki",
        url_hash="a" * 64,
        title="KI-Projekt",
        description="RAG Pipeline mit Python",
        skills=["Python"],
        start_date=None,
        start_asap=True,
        end_date=None,
        location="Remote",
        remote_status=RemoteStatus.REMOTE,
        posted_at=None,
        posted_at_precision=PostedPrecision.UNKNOWN,
        raw={"company": "Firma GmbH"},
    )


async def test_check_text_blacklist_fails_hard_rule_without_llm_call() -> None:
    matcher = _FakeMatcher(_llm())
    result = await _service(matcher, blacklist=["sap"]).check_text("SAP Berater gesucht")
    assert result.stage is EvaluationStage.HARD_RULE
    assert result.verdict is Verdict.NO_MATCH
    assert not result.passed and result.message is None
    assert result.reason == {"rule": "blacklist", "matched_term": "sap"}
    assert matcher.listing_texts == []  # 0 tokens spent


async def test_check_text_match_builds_message_with_the_checked_text() -> None:
    matcher = _FakeMatcher(_llm(score=75))
    result = await _service(matcher).check_text("Python Backend Projekt\nRAG und FastAPI")
    assert result.passed and result.stage is EvaluationStage.LLM
    assert result.score == 75 and result.threshold == THRESHOLD
    message = result.message
    assert message is not None
    assert message.title == "Python Backend Projekt"
    assert message.url == ""  # raw text has no listing URL
    assert message.description.startswith("Python Backend Projekt")
    assert message.reasons == ["Passt zum Profil"]
    assert matcher.listing_texts == ["Python Backend Projekt\nRAG und FastAPI"]


async def test_check_text_match_below_threshold_does_not_pass() -> None:
    result = await _service(_FakeMatcher(_llm(score=40))).check_text("Python Projekt")
    assert result.verdict is Verdict.MATCH  # the LLM said match ...
    assert not result.passed and result.message is None  # ... but under the threshold
    assert result.score == 40


async def test_check_text_no_match_keeps_reasons() -> None:
    result = await _service(_FakeMatcher(_llm("no_match", 20))).check_text("Java Projekt")
    assert result.verdict is Verdict.NO_MATCH and not result.passed
    assert result.reason.get("reasons") == ["Passt zum Profil"]


async def test_check_text_llm_error_is_flagged() -> None:
    result = await _service(_FakeMatcher(_llm("no_match", 0, is_error=True))).check_text("x")
    assert result.is_llm_error and not result.passed


async def test_check_parsed_match_carries_listing_fields() -> None:
    result = await _service(_FakeMatcher(_llm(score=90))).check_parsed(_parsed())
    message = result.message
    assert message is not None
    assert message.url == "https://www.freelancermap.de/projekt/ki"
    assert message.company == "Firma GmbH"
    assert message.start == "ASAP"
    assert message.skills == ["Python"]


async def test_check_stored_evaluates_the_db_listing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        listing, _ = await Repository(session).upsert_listing(
            Listing(
                source="freelancermap",
                external_url="https://www.freelancermap.de/projekt/db",
                url_hash="b" * 64,
                title="Gespeichertes Projekt",
                description="Python und RAG",
            )
        )
        listing_id = listing.id
    matcher = _FakeMatcher(_llm(score=70))
    result = await _service(matcher, session_factory=session_factory).check_stored(listing_id)
    assert result.passed
    assert result.message is not None
    assert result.message.url == "https://www.freelancermap.de/projekt/db"
    assert "Gespeichertes Projekt" in matcher.listing_texts[0]


async def test_check_stored_unknown_listing_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(_FakeMatcher(_llm()), session_factory=session_factory)
    with pytest.raises(ApplicationStateError):
        await service.check_stored(99999)

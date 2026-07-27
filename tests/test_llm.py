"""Tests for the stage 3 LLM matcher (OpenAI client fully mocked)."""

from datetime import date
from typing import Literal

import pytest

from project_pilot.errors import ConfigError
from project_pilot.evaluation.llm import (
    LlmEvaluation,
    LlmMatcher,
    LlmResponse,
    is_match_notifiable,
    load_prompt,
    render_listing,
)
from project_pilot.evaluation.schemas import MatchVerdict
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import PostedPrecision, RemoteStatus


class _FakeClient:
    def __init__(self, responses: list[LlmResponse | Exception]) -> None:
        self._responses: list[LlmResponse | Exception] = list(responses)
        self.calls = 0

    async def complete(self, *, model: str, system: str, user: str) -> LlmResponse:
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _verdict(verdict: Literal["match", "no_match"] = "match", score: int = 80) -> MatchVerdict:
    return MatchVerdict(
        verdict=verdict,
        score=score,
        reasons=["good fit"],
        matching_skills=["python"],
        missing_requirements=[],
        risk_flags=[],
    )


def _evaluation(verdict: MatchVerdict) -> LlmEvaluation:
    return LlmEvaluation(
        verdict=verdict,
        model="m",
        prompt_version="v",
        tokens_in=None,
        tokens_out=None,
        latency_ms=1,
        is_error=False,
    )


async def test_match_returns_verdict_and_metadata() -> None:
    client = _FakeClient([LlmResponse(verdict=_verdict(), tokens_in=100, tokens_out=20)])
    matcher = LlmMatcher(client, model="gpt-mini", prompt_template="SYS")
    result = await matcher.evaluate(profile_text="P", listing_text="L")
    assert result.is_match is True
    assert result.score == 80
    assert result.tokens_in == 100
    assert result.prompt_version == "match.v1"
    assert result.is_error is False
    assert client.calls == 1


async def test_no_match_verdict() -> None:
    client = _FakeClient(
        [LlmResponse(verdict=_verdict("no_match", 20), tokens_in=None, tokens_out=None)]
    )
    matcher = LlmMatcher(client, model="m", prompt_template="SYS")
    result = await matcher.evaluate(profile_text="P", listing_text="L")
    assert result.is_match is False
    assert result.is_error is False


async def test_schema_violation_retries_then_succeeds() -> None:
    client = _FakeClient(
        [
            LlmResponse(verdict=None, tokens_in=None, tokens_out=None),
            LlmResponse(verdict=_verdict(), tokens_in=10, tokens_out=5),
        ]
    )
    matcher = LlmMatcher(client, model="m", prompt_template="SYS")
    result = await matcher.evaluate(profile_text="P", listing_text="L")
    assert result.is_error is False
    assert result.is_match is True
    assert client.calls == 2


async def test_persistent_schema_violation_falls_back() -> None:
    client = _FakeClient(
        [
            LlmResponse(verdict=None, tokens_in=None, tokens_out=None),
            LlmResponse(verdict=None, tokens_in=None, tokens_out=None),
        ]
    )
    matcher = LlmMatcher(client, model="m", prompt_template="SYS")
    result = await matcher.evaluate(profile_text="P", listing_text="L")
    assert result.is_error is True
    assert result.is_match is False
    assert result.reason()["error"] == "llm_error"
    assert client.calls == 2


async def test_exception_then_success() -> None:
    client = _FakeClient(
        [RuntimeError("boom"), LlmResponse(verdict=_verdict(), tokens_in=1, tokens_out=1)]
    )
    matcher = LlmMatcher(client, model="m", prompt_template="SYS")
    result = await matcher.evaluate(profile_text="P", listing_text="L")
    assert result.is_error is False
    assert client.calls == 2


async def test_persistent_exception_falls_back() -> None:
    client = _FakeClient([RuntimeError("boom"), RuntimeError("boom2")])
    matcher = LlmMatcher(client, model="m", prompt_template="SYS")
    result = await matcher.evaluate(profile_text="P", listing_text="L")
    assert result.is_error is True
    assert result.reason()["verdict"] == "no_match"


def test_is_match_notifiable() -> None:
    match_high = _evaluation(_verdict("match", 70))
    assert is_match_notifiable(match_high, 60) is True
    assert is_match_notifiable(match_high, 80) is False
    no_match_high = _evaluation(_verdict("no_match", 95))
    assert is_match_notifiable(no_match_high, 60) is False


def test_render_listing_includes_fields() -> None:
    listing = ParsedListing(
        source="freelancermap",
        external_url="https://x/1",
        url_hash="h",
        title="Py Dev",
        description="Build async services",
        skills=["Python", "FastAPI"],
        start_date=None,
        start_asap=True,
        end_date=None,
        location="Remote",
        remote_status=RemoteStatus.REMOTE,
        posted_at=None,
        posted_at_precision=PostedPrecision.UNKNOWN,
        raw={},
    )
    text = render_listing(listing)
    assert "Py Dev" in text
    assert "Python" in text
    assert "ab sofort" in text
    assert "Reference:" not in text


def test_render_listing_carries_the_reference_number() -> None:
    listing = ParsedListing(
        source="freelancermap",
        external_url="https://x/3",
        url_hash="h3",
        title="Py Dev",
        description="Build async services",
        skills=[],
        start_date=None,
        start_asap=True,
        end_date=None,
        location=None,
        remote_status=RemoteStatus.REMOTE,
        posted_at=None,
        posted_at_precision=PostedPrecision.UNKNOWN,
        raw={"id": 3028498},
    )
    assert "Reference: 3028498" in render_listing(listing)


def test_load_prompt_reads_v1() -> None:
    text = load_prompt("match.v1")
    assert text.strip()
    assert "match" in text.lower()


def test_load_prompt_missing_raises() -> None:
    with pytest.raises(ConfigError):
        load_prompt("does-not-exist")


def test_render_listing_with_start_date() -> None:
    listing = ParsedListing(
        source="freelancermap",
        external_url="https://x/2",
        url_hash="h2",
        title="Data Eng",
        description="Azure work",
        skills=[],
        start_date=date(2026, 9, 1),
        start_asap=False,
        end_date=None,
        location=None,
        remote_status=RemoteStatus.ONSITE,
        posted_at=None,
        posted_at_precision=PostedPrecision.UNKNOWN,
        raw={},
    )
    text = render_listing(listing)
    assert "Start: 2026-09-01" in text
    assert "ab sofort" not in text

"""Tests for the stage 3 no-go guard over the model's own missing_requirements."""

from typing import Literal

from project_pilot.evaluation.nogo import NOGO_REASON, enforce_nogo, find_nogo_requirement
from project_pilot.evaluation.schemas import MatchVerdict

TERMS = ["java", "spring", "php", "wordpress", "django", "sap"]


def _verdict(
    *,
    verdict: Literal["match", "no_match"] = "match",
    score: int = 86,
    missing: list[str] | None = None,
    reasons: list[str] | None = None,
) -> MatchVerdict:
    return MatchVerdict(
        project_title="Full-Stack Developer (Frontend Focus - Angular)",
        verdict=verdict,
        score=score,
        reasons=reasons or ["strong Angular/RxJS fit"],
        matching_skills=["Angular", "RxJS"],
        missing_requirements=missing or [],
        risk_flags=[],
    )


def test_required_nogo_turns_a_match_into_no_match() -> None:
    """The reported case: Angular role that also requires Spring Boot and Java."""
    result, term = enforce_nogo(_verdict(missing=["AG Grid", "Spring Boot"]), TERMS)
    assert term == "spring"
    assert result.verdict == "no_match"
    assert result.score == 0
    assert result.reasons[0] == f"{NOGO_REASON}: spring"
    assert "strong Angular/RxJS fit" in result.reasons


def test_plain_java_requirement_is_a_nogo() -> None:
    _result, term = enforce_nogo(_verdict(missing=["Java (Version 11 oder höher)"]), TERMS)
    assert term == "java"


def test_javascript_is_not_java() -> None:
    result, term = enforce_nogo(_verdict(missing=["JavaScript build tooling"]), TERMS)
    assert term is None
    assert result.verdict == "match"
    assert result.score == 86


def test_clean_match_is_untouched() -> None:
    verdict = _verdict(missing=["AG Grid"])
    result, term = enforce_nogo(verdict, TERMS)
    assert term is None
    assert result is verdict


def test_nogo_only_as_context_does_not_fire() -> None:
    """A frontend role against a Java backend never lists Java as a requirement."""
    verdict = _verdict(missing=[], reasons=["frontend-only role against a Java backend"])
    result, term = enforce_nogo(verdict, TERMS)
    assert term is None
    assert result.verdict == "match"


def test_no_match_verdict_is_left_alone() -> None:
    verdict = _verdict(verdict="no_match", score=10, missing=["Spring Boot"])
    result, term = enforce_nogo(verdict, TERMS)
    assert term is None
    assert result is verdict


def test_empty_term_list_disables_the_guard() -> None:
    result, term = enforce_nogo(_verdict(missing=["Spring Boot"]), [])
    assert term is None
    assert result.verdict == "match"


def test_blank_terms_are_ignored() -> None:
    assert find_nogo_requirement(_verdict(missing=["Spring Boot"]), ["  ", ""]) is None


def test_terms_are_matched_case_insensitively_and_trimmed() -> None:
    assert find_nogo_requirement(_verdict(missing=["SAP-Integration"]), [" sap "]) == "sap"

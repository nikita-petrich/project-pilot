"""Tests for the stage 2 hard-rule engine."""

from project_pilot.evaluation.rules import apply_hard_rules
from project_pilot.profile_loader import ProfileConstraints


def _constraints(
    *, blacklist: list[str] | None = None, must_have: list[str] | None = None
) -> ProfileConstraints:
    return ProfileConstraints(blacklist=blacklist or [], must_have=must_have or [])


def test_blacklist_hit() -> None:
    result = apply_hard_rules("We use WordPress heavily", _constraints(blacklist=["wordpress"]))
    assert result.passed is False
    assert result.reason == {"rule": "blacklist", "matched_term": "wordpress"}


def test_blacklist_case_insensitive() -> None:
    assert apply_hard_rules("SAP shop", _constraints(blacklist=["sap"])).passed is False


def test_word_boundary_java_not_in_javascript() -> None:
    assert (
        apply_hard_rules("Senior JavaScript role", _constraints(blacklist=["java"])).passed is True
    )
    assert apply_hard_rules("Senior Java role", _constraints(blacklist=["java"])).passed is False


def test_special_tokens_csharp_cpp_dotnet() -> None:
    assert apply_hard_rules("C# developer", _constraints(blacklist=["c#"])).passed is False
    assert apply_hard_rules("call me maybe", _constraints(blacklist=["c#"])).passed is True
    assert apply_hard_rules("C++ engineer", _constraints(blacklist=["c++"])).passed is False
    assert apply_hard_rules(".NET stack", _constraints(blacklist=[".net"])).passed is False


def test_must_have_satisfied() -> None:
    result = apply_hard_rules("Python and asyncio", _constraints(must_have=["python", "go"]))
    assert result.passed is True


def test_must_have_missing() -> None:
    result = apply_hard_rules("Ruby on Rails", _constraints(must_have=["python", "go"]))
    assert result.passed is False
    assert result.reason["rule"] == "must_have"


def test_empty_constraints_pass() -> None:
    assert apply_hard_rules("anything at all", _constraints()).passed is True


def test_blacklist_precedes_must_have() -> None:
    result = apply_hard_rules(
        "Python with WordPress",
        _constraints(blacklist=["wordpress"], must_have=["python"]),
    )
    assert result.passed is False
    assert result.reason["rule"] == "blacklist"

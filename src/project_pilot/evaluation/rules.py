"""Stage 2 deterministic rule engine over constraints.yaml."""

import re
from dataclasses import dataclass

from project_pilot.profile_loader import ProfileConstraints


@dataclass(frozen=True, slots=True)
class RuleResult:
    passed: bool
    reason: dict[str, object]


def term_regex(term: str) -> re.Pattern[str]:
    """Match ``term`` as a whole token.

    The boundary is ``[a-z0-9]`` lookaround rather than ``\\b`` so terms that end or
    start with punctuation work: ``c#``, ``c++`` and ``.net`` match as tokens, while
    ``java`` does not match inside ``javascript``.
    """
    escaped = re.escape(term)
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


def _matches(term: str, text: str) -> bool:
    cleaned = term.strip()
    return bool(cleaned) and term_regex(cleaned).search(text) is not None


def apply_hard_rules(text: str, constraints: ProfileConstraints) -> RuleResult:
    """Return the first failing rule, or a pass. Zero model tokens."""
    for term in constraints.blacklist:
        if _matches(term, text):
            return RuleResult(
                passed=False, reason={"rule": "blacklist", "matched_term": term.strip()}
            )

    required = [term.strip() for term in constraints.must_have if term.strip()]
    if required and not any(term_regex(term).search(text) for term in required):
        return RuleResult(passed=False, reason={"rule": "must_have", "required": required})

    return RuleResult(passed=True, reason={})

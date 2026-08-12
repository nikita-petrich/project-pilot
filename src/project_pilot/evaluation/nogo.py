"""Stage 3 post-check: force ``no_match`` on a required no-go technology.

The profile's technology no-gos (Java, PHP, WordPress, Django, SAP) are
context-dependent, so they cannot be blacklisted on the listing text: a
frontend-only role against a Java backend, or a migration away from PHP, is
wanted. What is *not* wanted is a role that expects the candidate to build in one
of them — and that distinction only exists after the listing has been read.

The signal used here is the model's own structured output: when it reports a
no-go technology under ``missing_requirements``, it has already judged that the
listing requires the candidate to have it and that the profile does not cover it.
That is exactly the disqualifying case, so the verdict is overridden
deterministically instead of relying on the model to weigh the no-go correctly.
"""

from collections.abc import Sequence

from project_pilot.evaluation.rules import term_regex
from project_pilot.evaluation.schemas import MatchVerdict

# Prefixed to the model's own reasons so the stored evaluation shows why a verdict
# that arrived as "match" is recorded as "no_match".
NOGO_REASON = "profile no-go technology required by the listing"


def find_nogo_requirement(verdict: MatchVerdict, terms: Sequence[str]) -> str | None:
    """The first no-go term the model reports as an uncovered listing requirement.

    Terms match as whole tokens, so ``java`` does not fire on "JavaScript" and
    ``spring`` does fire on "Spring Boot".
    """
    for requirement in verdict.missing_requirements:
        for term in terms:
            cleaned = term.strip()
            if cleaned and term_regex(cleaned).search(requirement):
                return cleaned
    return None


def enforce_nogo(verdict: MatchVerdict, terms: Sequence[str]) -> tuple[MatchVerdict, str | None]:
    """Return the verdict to store plus the no-go term that overrode it, if any.

    A verdict that is already ``no_match`` is returned untouched: there is nothing
    to override, and the term would only add noise to its reasons.
    """
    if verdict.verdict != "match":
        return verdict, None
    term = find_nogo_requirement(verdict, terms)
    if term is None:
        return verdict, None
    return (
        verdict.model_copy(
            update={
                "verdict": "no_match",
                # The model's confidence was about applying; with a required no-go
                # there is nothing to apply to, so it drops rather than lingering
                # as a high score on a no_match row in reporting.
                "score": 0,
                "reasons": [f"{NOGO_REASON}: {term}", *verdict.reasons],
            }
        ),
        term,
    )

"""Golden-set eval: does the live judgment still decide these cases correctly?

Runs the real ``LlmMatcher`` (live prompt, live no-go post-check, real API calls
against whichever ``LLM_PROVIDER`` is configured) over ``golden.jsonl`` and fails
when accuracy drops below the baseline.
This is the regression gate for any change to the judgment: prompt edits, skill
rewires, model swaps.

Excluded from the normal suite (``-m "not eval"`` in addopts) because it costs
tokens and needs a key; run it with ``uv run pytest -m eval``. Without the selected
provider's key and ``LLM_MODEL`` it skips instead of failing.

This is the gate for a provider switch: the prompt is tuned against one model, so
the set must be re-run (and the threshold re-checked) whenever ``LLM_PROVIDER`` or
``LLM_MODEL`` changes.

The set covers German and English listings alike, because the prompt judges both
and a language must never decide a verdict on its own.

The starter labels were set by the AI from the profile's rules and await Nik's
review; grow the set from real ``evaluations`` rows over time.
"""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from project_pilot.config import load_settings
from project_pilot.evaluation.llm import (
    LlmMatcher,
    is_match_notifiable,
    load_prompt,
    structured_client,
)
from project_pilot.profile_loader import ProfileService
from project_pilot.profile_source import WebProfileSource


def _configured_key() -> str:
    """The key of whichever provider is selected, so the skip matches the real need."""
    provider = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower()
    return os.environ.get("ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY", "")


pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not (_configured_key() and os.environ.get("LLM_MODEL")),
        reason="eval needs LLM_MODEL and the API key of the configured LLM_PROVIDER",
    ),
]

GOLDEN = Path(__file__).with_name("golden.jsonl")
ACCURACY_BASELINE = 0.85
MATCH_THRESHOLD = 60  # the production default; the eval measures the shipped decision
MAX_CONCURRENCY = 4


@dataclass(frozen=True, slots=True)
class GoldenCase:
    id: str
    description: str
    expected_verdict: str
    note: str


def _cases() -> list[GoldenCase]:
    cases = []
    for line in GOLDEN.read_text().splitlines():
        if line.strip():
            data = json.loads(line)
            cases.append(GoldenCase(**data))
    return cases


async def test_golden_set_accuracy() -> None:
    # The live profile, exactly as the worker reads it: the eval is worthless if it
    # judges against a copy the worker would never see.
    settings = load_settings()
    profile = await ProfileService(
        Path("profile"),
        WebProfileSource(base_url=settings.profile_url, locale=settings.profile_locale),
    ).load()
    # Built from the real settings, so the eval judges exactly the provider, key and
    # model the worker would use — the whole point of the gate.
    credentials = settings.require_llm()
    matcher = LlmMatcher(
        structured_client(credentials),
        model=credentials.model,
        prompt_template=load_prompt(),
        nogo_terms=profile.constraints.nogo_technologies,
    )
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def judge(case: GoldenCase) -> tuple[GoldenCase, str, bool]:
        async with semaphore:
            evaluation = await matcher.evaluate(
                profile_text=profile.text, listing_text=case.description
            )
        if evaluation.is_error:
            pytest.fail(f"LLM error on {case.id}: {evaluation.reason()}")
        predicted = "match" if is_match_notifiable(evaluation, MATCH_THRESHOLD) else "no_match"
        return case, predicted, predicted == case.expected_verdict

    results = await asyncio.gather(*(judge(case) for case in _cases()))
    wrong = [(case, predicted) for case, predicted, ok in results if not ok]
    accuracy = 1 - len(wrong) / len(results)

    report = "\n".join(
        f"  {case.id}: expected {case.expected_verdict}, got {predicted} ({case.note})"
        for case, predicted in wrong
    )
    assert accuracy >= ACCURACY_BASELINE, (
        f"golden-set accuracy {accuracy:.2f} below baseline {ACCURACY_BASELINE}:\n{report}"
    )

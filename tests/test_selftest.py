"""Tests for the end-to-end self-test service behind ``project-pilot test-match``."""

import pytest

from project_pilot.evaluation.check import CheckResult
from project_pilot.models import EvaluationStage, Verdict
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.slack import Block, PostedMessage
from project_pilot.selftest import (
    DEMO_LISTING,
    SelfTestService,
    format_selftest,
)


def _match_result(*, passed: bool = True, score: int = 82) -> CheckResult:
    message = MatchMessage(title="Demo", url="https://example.test/p/1", score=score)
    return CheckResult(
        title="Demo",
        stage=EvaluationStage.LLM,
        verdict=Verdict.MATCH if passed else Verdict.NO_MATCH,
        passed=passed,
        score=score,
        threshold=60,
        reason={},
        message=message if passed else None,
        is_llm_error=False,
    )


class _FakeChecker:
    def __init__(self, result: CheckResult | Exception) -> None:
        self._result = result
        self.text_calls: list[str] = []
        self.stored_calls: list[int] = []

    async def check_text(self, text: str) -> CheckResult:
        self.text_calls.append(text)
        return self._unwrap()

    async def check_stored(self, listing_id: int) -> CheckResult:
        self.stored_calls.append(listing_id)
        return self._unwrap()

    def _unwrap(self) -> CheckResult:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakePoster:
    def __init__(self, *, posted: bool = True) -> None:
        self._posted = posted
        self.calls: list[tuple[list[Block], str]] = []

    async def post_blocks(
        self, blocks: list[Block], text: str, *, thread_ts: str | None = None
    ) -> object | None:
        self.calls.append((blocks, text))
        return PostedMessage(channel="C1", ts="1.0") if self._posted else None


class _FakeNotifier:
    def __init__(self, *, sent: bool = True) -> None:
        self._sent = sent
        self.calls: list[tuple[MatchMessage, int]] = []

    async def send_match(self, message: MatchMessage, *, listing_id: int) -> bool:
        self.calls.append((message, listing_id))
        return self._sent


def _service(
    checker: _FakeChecker, poster: _FakePoster, notifier: _FakeNotifier
) -> SelfTestService:
    return SelfTestService(
        checker=checker, poster=poster, notifier=notifier, profile_hash="abc123def456789"
    )


@pytest.mark.asyncio
async def test_demo_listing_is_evaluated_and_posted_as_a_check_verdict() -> None:
    checker, poster, notifier = _FakeChecker(_match_result()), _FakePoster(), _FakeNotifier()

    report = await _service(checker, poster, notifier).run()

    assert report.ok
    assert checker.text_calls == [DEMO_LISTING]
    assert len(poster.calls) == 1
    assert notifier.calls == []  # no listing id, so no match card with live buttons


@pytest.mark.asyncio
async def test_custom_text_is_used_instead_of_the_demo() -> None:
    checker, poster, notifier = _FakeChecker(_match_result()), _FakePoster(), _FakeNotifier()

    await _service(checker, poster, notifier).run(text="Node.js Projekt")

    assert checker.text_calls == ["Node.js Projekt"]


@pytest.mark.asyncio
async def test_listing_id_posts_the_real_match_card() -> None:
    checker, poster, notifier = _FakeChecker(_match_result()), _FakePoster(), _FakeNotifier()

    report = await _service(checker, poster, notifier).run(listing_id=42)

    assert report.ok
    assert checker.stored_calls == [42]
    assert [listing_id for _, listing_id in notifier.calls] == [42]
    assert poster.calls == []


@pytest.mark.asyncio
async def test_a_no_match_still_passes_because_the_chain_worked() -> None:
    checker = _FakeChecker(_match_result(passed=False, score=12))
    poster, notifier = _FakePoster(), _FakeNotifier()

    report = await _service(checker, poster, notifier).run()

    assert report.ok
    assert len(poster.calls) == 1
    assert "no match" in format_selftest(report)


@pytest.mark.asyncio
async def test_an_llm_error_fails_the_evaluation_step_but_still_reports_slack() -> None:
    result = _match_result(passed=False)
    checker = _FakeChecker(
        CheckResult(
            title=result.title,
            stage=result.stage,
            verdict=result.verdict,
            passed=False,
            score=None,
            threshold=60,
            reason={},
            message=None,
            is_llm_error=True,
        )
    )
    poster, notifier = _FakePoster(), _FakeNotifier()

    report = await _service(checker, poster, notifier).run()

    assert not report.ok
    steps = {step.name: step.ok for step in report.steps}
    assert steps == {"profile": True, "evaluation": False, "slack": True}


@pytest.mark.asyncio
async def test_an_evaluation_crash_is_reported_instead_of_raised() -> None:
    checker = _FakeChecker(RuntimeError("openai unreachable"))
    poster, notifier = _FakePoster(), _FakeNotifier()

    report = await _service(checker, poster, notifier).run()

    assert not report.ok
    assert report.result is None
    assert [step.name for step in report.steps] == ["profile", "evaluation"]
    assert "openai unreachable" in format_selftest(report)


@pytest.mark.asyncio
async def test_a_rejected_slack_post_fails_the_run() -> None:
    checker = _FakeChecker(_match_result())
    poster, notifier = _FakePoster(posted=False), _FakeNotifier()

    report = await _service(checker, poster, notifier).run()

    assert not report.ok
    assert "FAIL  slack" in format_selftest(report)


@pytest.mark.asyncio
async def test_a_hard_rule_stop_counts_as_a_working_chain() -> None:
    checker = _FakeChecker(
        CheckResult(
            title="Blocked",
            stage=EvaluationStage.HARD_RULE,
            verdict=Verdict.NO_MATCH,
            passed=False,
            score=None,
            threshold=60,
            reason={"rule": "blacklist", "matched_term": "wordpress"},
            message=None,
            is_llm_error=False,
        )
    )
    poster, notifier = _FakePoster(), _FakeNotifier()

    report = await _service(checker, poster, notifier).run()

    assert report.ok
    assert "hard rule" in format_selftest(report)

"""Self-test: the report reflects each link, and the fire step proves the channel."""

from project_pilot.evaluation.check import CheckResult
from project_pilot.models import EvaluationStage, Verdict
from project_pilot.notification.messages import MatchMessage
from project_pilot.selftest import SelfTestService, format_selftest


class _FakeChecker:
    def __init__(self, result: CheckResult) -> None:
        self._result = result

    async def check_text(self, text: str) -> CheckResult:
        return self._result

    async def check_stored(self, listing_id: int) -> CheckResult:
        return self._result


class _FakeFire:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.matches: list[MatchMessage] = []
        self.warnings: list[str] = []

    async def fire(self, message: MatchMessage) -> str | None:
        self.matches.append(message)
        return "https://claude.ai/code/session_01T" if self.ok else None

    async def fire_warning(self, text: str) -> bool:
        self.warnings.append(text)
        return self.ok


def _result(*, passed: bool) -> CheckResult:
    message = MatchMessage(title="T", url="https://x/p", score=80) if passed else None
    return CheckResult(
        title="T",
        stage=EvaluationStage.LLM,
        verdict=Verdict.MATCH if passed else Verdict.NO_MATCH,
        passed=passed,
        score=80 if passed else 10,
        threshold=60,
        reason={},
        message=message,
        is_llm_error=False,
    )


def _service(result: CheckResult, fire: _FakeFire) -> SelfTestService:
    return SelfTestService(checker=_FakeChecker(result), fire=fire, profile_hash="abc123def456")


async def test_match_opens_thread() -> None:
    fire = _FakeFire()
    report = await _service(_result(passed=True), fire).run()
    assert report.ok
    assert len(fire.matches) == 1
    assert "session_01T" in format_selftest(report)


async def test_no_match_proves_channel_via_warning() -> None:
    fire = _FakeFire()
    report = await _service(_result(passed=False), fire).run()
    assert report.ok
    assert fire.matches == []
    assert len(fire.warnings) == 1


async def test_failed_fire_fails_the_report() -> None:
    fire = _FakeFire(ok=False)
    report = await _service(_result(passed=True), fire).run()
    assert not report.ok
    assert "FAIL" in format_selftest(report)

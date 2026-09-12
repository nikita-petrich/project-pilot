"""Self-test: the report reflects each link, and the push step proves the channel."""

from project_pilot.evaluation.check import CheckResult
from project_pilot.models import EvaluationStage, Verdict
from project_pilot.notification.messages import MatchMessage
from project_pilot.selftest import SelfTestService, format_selftest


class _FakeChecker:
    def __init__(self, result: CheckResult) -> None:
        self._result = result
        self.urls: list[str] = []
        self.stored_ids: list[int] = []
        self.latest_calls = 0

    async def check_text(self, text: str, *, url: str = "") -> CheckResult:
        self.urls.append(url)
        return self._result

    async def check_stored(self, listing_id: int) -> CheckResult:
        self.stored_ids.append(listing_id)
        return self._result

    async def check_latest(self) -> CheckResult:
        self.latest_calls += 1
        return self._result


class _FakeNotifier:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.matches: list[MatchMessage] = []
        self.warnings: list[str] = []

    async def notify(self, message: MatchMessage) -> int | None:
        self.matches.append(message)
        # The message's id, or nothing at all when the send failed.
        return 5150 if self.ok else None

    async def notify_warning(self, text: str) -> bool:
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


def _service(result: CheckResult, notifier: _FakeNotifier) -> SelfTestService:
    return SelfTestService(
        checker=_FakeChecker(result), notifier=notifier, profile_hash="abc123def456"
    )


async def test_match_pushes_card() -> None:
    notifier = _FakeNotifier()
    report = await _service(_result(passed=True), notifier).run()
    assert report.ok
    assert len(notifier.matches) == 1
    assert "match card pushed" in format_selftest(report)


async def test_no_match_proves_channel_via_warning() -> None:
    notifier = _FakeNotifier()
    report = await _service(_result(passed=False), notifier).run()
    assert report.ok
    assert notifier.matches == []
    assert len(notifier.warnings) == 1


async def test_failed_push_fails_the_report() -> None:
    notifier = _FakeNotifier(ok=False)
    report = await _service(_result(passed=True), notifier).run()
    assert not report.ok
    assert "FAIL" in format_selftest(report)


async def test_run_forwards_the_given_url_to_check_text() -> None:
    checker = _FakeChecker(_result(passed=True))
    service = SelfTestService(checker=checker, notifier=_FakeNotifier(), profile_hash="abc123")
    await service.run(text="Python Projekt", url="https://www.freelancermap.de/projekt/123")
    assert checker.urls == ["https://www.freelancermap.de/projekt/123"]


async def test_run_with_no_input_checks_the_latest_stored_listing() -> None:
    checker = _FakeChecker(_result(passed=True))
    service = SelfTestService(checker=checker, notifier=_FakeNotifier(), profile_hash="abc123")
    await service.run()
    assert checker.latest_calls == 1
    assert checker.urls == []
    assert checker.stored_ids == []


async def test_run_with_listing_id_checks_that_stored_listing() -> None:
    checker = _FakeChecker(_result(passed=True))
    service = SelfTestService(checker=checker, notifier=_FakeNotifier(), profile_hash="abc123")
    await service.run(listing_id=42)
    assert checker.stored_ids == [42]
    assert checker.latest_calls == 0

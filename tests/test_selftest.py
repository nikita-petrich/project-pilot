"""Self-test: the report reflects each link, and the push step proves the channel."""

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


class _FakeNotifier:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.matches: list[MatchMessage] = []
        self.session_urls: list[str | None] = []
        self.warnings: list[str] = []

    async def notify(self, message: MatchMessage, *, session_url: str | None = None) -> int | None:
        self.matches.append(message)
        self.session_urls.append(session_url)
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


class _FakeOpener:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.fired: list[MatchMessage] = []

    async def open_session(self, message: MatchMessage) -> str | None:
        self.fired.append(message)
        return "https://claude.ai/code/session_01X" if self.ok else None


def _service(
    result: CheckResult, notifier: _FakeNotifier, opener: _FakeOpener | None = None
) -> SelfTestService:
    return SelfTestService(
        checker=_FakeChecker(result),
        notifier=notifier,
        opener=opener or _FakeOpener(),
        profile_hash="abc123def456",
    )


async def test_match_pushes_card() -> None:
    notifier = _FakeNotifier()
    report = await _service(_result(passed=True), notifier).run()
    assert report.ok
    assert len(notifier.matches) == 1
    # The card links to the session the fire opened.
    assert notifier.session_urls == ["https://claude.ai/code/session_01X"]
    assert "claude session opened" in format_selftest(report)
    assert "match card pushed" in format_selftest(report)


async def test_no_match_proves_channel_via_warning() -> None:
    notifier = _FakeNotifier()
    opener = _FakeOpener()
    report = await _service(_result(passed=False), notifier, opener).run()
    assert report.ok
    assert notifier.matches == []
    assert len(notifier.warnings) == 1
    assert opener.fired == []  # a routine run is not free; a no-match opens none


async def test_a_failed_fire_fails_the_report_but_the_card_still_goes_out() -> None:
    notifier = _FakeNotifier()
    report = await _service(_result(passed=True), notifier, _FakeOpener(ok=False)).run()
    assert not report.ok
    assert "FAIL  session" in format_selftest(report)
    assert notifier.session_urls == [None]  # sent, without a Bewerben button


async def test_failed_push_fails_the_report() -> None:
    notifier = _FakeNotifier(ok=False)
    report = await _service(_result(passed=True), notifier).run()
    assert not report.ok
    assert "FAIL" in format_selftest(report)

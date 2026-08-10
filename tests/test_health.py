"""Tests for failure classification and the throttled operator alerter."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from project_pilot.health import (
    LLM_COMPONENT,
    HealthAlerter,
    HealthIssue,
    HealthKind,
    classify_llm_error,
    llm_issue,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
MODEL = "gpt-tiny-42"


def _api_error(status: int, code: str | None = None, message: str = "boom") -> APIStatusError:
    """A real OpenAI SDK error, built the way the SDK builds it from a response."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body: dict[str, object] = {"message": message}
    if code is not None:
        body["code"] = code
    return APIStatusError(message, response=httpx.Response(status, request=request), body=body)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_api_error(404, "model_not_found"), HealthKind.MODEL_NOT_FOUND),
        (_api_error(404), HealthKind.MODEL_NOT_FOUND),
        (_api_error(401, "invalid_api_key"), HealthKind.AUTH),
        (_api_error(403), HealthKind.AUTH),
        (_api_error(429, "insufficient_quota"), HealthKind.QUOTA),
        (_api_error(429, "rate_limit_exceeded"), HealthKind.RATE_LIMIT),
        (_api_error(500), HealthKind.UPSTREAM),
        (_api_error(503), HealthKind.UPSTREAM),
        (ValueError("something else"), HealthKind.UNKNOWN),
    ],
)
def test_classify_maps_provider_errors_to_actionable_kinds(
    error: Exception, expected: HealthKind
) -> None:
    assert classify_llm_error(error, model=MODEL).kind is expected


def test_classify_detects_a_dead_connection() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    error = APIConnectionError(request=request)

    assert classify_llm_error(error, model=MODEL).kind is HealthKind.CONNECTION


def test_quota_code_wins_over_the_status_code() -> None:
    """An exhausted account can arrive as a 404; the code is the more specific signal."""
    issue = classify_llm_error(_api_error(404, "insufficient_quota"), model=MODEL)

    assert issue.kind is HealthKind.QUOTA


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        (HealthKind.MODEL_NOT_FOUND, False),
        (HealthKind.AUTH, False),
        (HealthKind.QUOTA, False),
        (HealthKind.RATE_LIMIT, True),
        (HealthKind.CONNECTION, True),
        (HealthKind.UPSTREAM, True),
        (HealthKind.SCHEMA, True),
        (HealthKind.UNKNOWN, True),
    ],
)
def test_only_transient_failures_are_worth_repeating(kind: HealthKind, retryable: bool) -> None:
    assert llm_issue(kind, model=MODEL).is_retryable is retryable


def test_message_names_the_model_the_setting_and_the_cost() -> None:
    message = classify_llm_error(_api_error(404, "model_not_found"), model=MODEL).as_message()

    assert MODEL in message
    assert "LLM_MODEL" in message
    assert "llm_error" in message  # the impact: no match alert can fire
    assert "APIStatusError" in message  # the provider's own words survive


def test_quota_message_says_to_top_up_the_account() -> None:
    message = classify_llm_error(_api_error(429, "insufficient_quota"), model=MODEL).as_message()

    assert "out of credit" in message


def test_detail_is_trimmed_to_one_backtick_free_line() -> None:
    issue = classify_llm_error(ValueError("a `quoted`\nmultiline " + "x" * 500), model=MODEL)

    assert "\n" not in issue.detail
    assert "`" not in issue.detail
    assert len(issue.detail) <= 300


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


def _issue(kind: HealthKind = HealthKind.MODEL_NOT_FOUND) -> HealthIssue:
    return llm_issue(kind, model=MODEL, detail="boom")


async def test_alerter_sends_once_and_then_stays_quiet() -> None:
    recorder = _Recorder()
    alerter = HealthAlerter(recorder, repeat_after=timedelta(hours=6))

    await alerter.failed(_issue(), now=NOW)
    await alerter.failed(_issue(), now=NOW + timedelta(minutes=15))
    await alerter.failed(_issue(), now=NOW + timedelta(hours=5))

    assert len(recorder.sent) == 1


async def test_alerter_reminds_once_the_repeat_window_has_passed() -> None:
    recorder = _Recorder()
    alerter = HealthAlerter(recorder, repeat_after=timedelta(hours=6))

    await alerter.failed(_issue(), now=NOW)
    await alerter.failed(_issue(), now=NOW + timedelta(hours=6, minutes=1))

    assert len(recorder.sent) == 2


async def test_a_different_cause_alerts_immediately() -> None:
    """Model fixed but the account is empty: a new problem must not inherit the window."""
    recorder = _Recorder()
    alerter = HealthAlerter(recorder)

    await alerter.failed(_issue(HealthKind.MODEL_NOT_FOUND), now=NOW)
    await alerter.failed(_issue(HealthKind.QUOTA), now=NOW + timedelta(minutes=1))

    assert len(recorder.sent) == 2
    assert "out of credit" in recorder.sent[1]


async def test_recovery_is_announced_once() -> None:
    recorder = _Recorder()
    alerter = HealthAlerter(recorder)

    await alerter.failed(_issue(), now=NOW)
    await alerter.recovered(LLM_COMPONENT)
    await alerter.recovered(LLM_COMPONENT)

    assert len(recorder.sent) == 2
    assert recorder.sent[1].startswith("✅")


async def test_recovery_is_silent_when_nothing_was_broken() -> None:
    recorder = _Recorder()

    await HealthAlerter(recorder).recovered(LLM_COMPONENT)

    assert recorder.sent == []


async def test_a_problem_that_returns_after_recovery_alerts_again() -> None:
    recorder = _Recorder()
    alerter = HealthAlerter(recorder, repeat_after=timedelta(hours=6))

    await alerter.failed(_issue(), now=NOW)
    await alerter.recovered(LLM_COMPONENT)
    await alerter.failed(_issue(), now=NOW + timedelta(minutes=30))

    assert len(recorder.sent) == 3

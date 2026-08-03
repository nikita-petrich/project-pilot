"""Tests for the CLI (test-notify), with the Slack Web API faked."""

import pytest
from typer.testing import CliRunner

from project_pilot.cli import app

runner = CliRunner()


class _FakeResp:
    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def get(self, key: str, default: object = None, /) -> object:
        return self._data.get(key, default)


class _FakeWeb:
    def __init__(self, *, token: str) -> None:
        self.token = token

    async def chat_postMessage(self, **kwargs: object) -> _FakeResp:  # noqa: N802 - slack_sdk name
        return _FakeResp({"ok": True, "ts": "1.2", "channel": kwargs.get("channel")})

    async def chat_update(self, **kwargs: object) -> _FakeResp:
        return _FakeResp({"ok": True})


def _set_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1")
    monkeypatch.setenv("SLACK_CHANNEL", "C0123")


def test_test_notify_posts_to_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_slack(monkeypatch)
    monkeypatch.setattr("project_pilot.cli.AsyncWebClient", _FakeWeb)
    result = runner.invoke(app, ["test-notify"])
    assert result.exit_code == 0
    assert "sent" in result.stdout


def test_test_notify_requires_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["test-notify"])
    assert result.exit_code != 0


def test_log_level_setting_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    _set_slack(monkeypatch)
    monkeypatch.setattr("project_pilot.cli.AsyncWebClient", _FakeWeb)
    monkeypatch.setenv("LOG_LEVEL", "warning")
    previous = logging.getLogger().level
    try:
        result = runner.invoke(app, ["test-notify"])
        assert result.exit_code == 0
        assert logging.getLogger().level == logging.WARNING
    finally:
        logging.getLogger().setLevel(previous)

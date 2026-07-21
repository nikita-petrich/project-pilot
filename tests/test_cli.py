"""Tests for the CLI (test-notify), with the Telegram API mocked."""

import httpx
import pytest
import respx
from typer.testing import CliRunner

from project_pilot.cli import app

runner = CliRunner()


@respx.mock
def test_test_notify_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok:123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    respx.post("https://api.telegram.org/bottok:123/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    result = runner.invoke(app, ["test-notify"])
    assert result.exit_code == 0
    assert "sent" in result.stdout


def test_test_notify_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    result = runner.invoke(app, ["test-notify"])
    assert result.exit_code != 0

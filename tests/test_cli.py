"""CLI tests: settings application and command guards, no live services."""

import logging

import pytest
from typer.testing import CliRunner

from project_pilot.cli import app

runner = CliRunner()


def test_log_level_setting_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    previous = logging.getLogger().level
    try:
        # test-match aborts on the missing Telegram config, but only after the
        # log level from the environment has been applied - which is what we assert.
        result = runner.invoke(app, ["test-match"])
        assert result.exit_code != 0
        assert logging.getLogger().level == logging.WARNING
    finally:
        logging.getLogger().setLevel(previous)


def test_test_match_requires_telegram_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    result = runner.invoke(app, ["test-match"])
    assert result.exit_code != 0


def test_test_match_rejects_url_with_listing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    result = runner.invoke(app, ["test-match", "--listing-id", "1", "--url", "https://x/p"])
    assert result.exit_code != 0
    assert "--url only applies to pasted text" in result.output


def test_enrich_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENRICHMENT_ENABLED", raising=False)
    result = runner.invoke(app, ["enrich", "ACME GmbH"])
    assert result.exit_code != 0

"""CLI tests: settings application and command guards, no live services."""

import logging

import pytest
from typer.testing import CliRunner

from project_pilot.cli import app

runner = CliRunner()


def test_log_level_setting_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.delenv("CLAUDE_ROUTINE_FIRE_URL", raising=False)
    previous = logging.getLogger().level
    try:
        # test-match aborts on the missing fire config, but only after the log
        # level from the environment has been applied - which is what we assert.
        result = runner.invoke(app, ["test-match"])
        assert result.exit_code != 0
        assert logging.getLogger().level == logging.WARNING
    finally:
        logging.getLogger().setLevel(previous)


def test_test_match_requires_fire_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_ROUTINE_FIRE_URL", raising=False)
    monkeypatch.delenv("CLAUDE_ROUTINE_TOKEN", raising=False)
    result = runner.invoke(app, ["test-match"])
    assert result.exit_code != 0


def test_enrich_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENRICHMENT_ENABLED", raising=False)
    result = runner.invoke(app, ["enrich", "ACME GmbH"])
    assert result.exit_code != 0

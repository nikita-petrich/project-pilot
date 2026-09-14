"""CLI tests: settings application and command guards, no live services."""

import json
import logging

import pytest
import respx
from typer.testing import CliRunner

from project_pilot.cli import _fetch_profile, app, silence_request_logging
from project_pilot.config import Settings
from project_pilot.errors import ProfileUnavailableError

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


@respx.mock
async def test_an_unreachable_profile_stops_the_work_and_says_so_on_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole failure mode this replaces: a worker that has quietly stopped
    # scanning looks exactly like a quiet week, so silence is the one answer that
    # must not happen. No fallback to an older profile either — see
    # profile_source.py.
    monkeypatch.setenv("PROFILE_URL", "https://profile.test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AAtest-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    respx.get("https://profile.test/en.md").respond(503)
    warning = respx.post("https://api.telegram.org/bot123456:AAtest-token/sendMessage").respond(
        200, json={"ok": True}
    )

    with pytest.raises(ProfileUnavailableError):
        await _fetch_profile(Settings())

    assert warning.called
    assert "Profil nicht abrufbar" in json.loads(warning.calls.last.request.content)["text"]


def test_request_urls_are_kept_out_of_the_log() -> None:
    # The Telegram bot token sits inside the request URL, and httpx logs that URL
    # at INFO — so the secret would land in `docker compose logs`.
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.INFO)
    silence_request_logging()
    for name in ("httpx", "httpx2", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING

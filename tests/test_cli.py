"""CLI tests: settings application and command guards, no live services."""

import json
import logging

import pytest
import respx
from typer.testing import CliRunner

from project_pilot.cli import (
    _build_pipeline,
    _wait_for_profile,
    app,
    silence_request_logging,
)
from project_pilot.config import Settings
from project_pilot.errors import ProfileUnavailableError
from project_pilot.profile_loader import Profile, ProfileConstraints

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


_TG_SEND = "https://api.telegram.org/bot123456:AAtest-token/sendMessage"


def _telegram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILE_URL", "https://profile.test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AAtest-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")


def _profile() -> Profile:
    return Profile(text="# Me", constraints=ProfileConstraints(), profile_hash="h" * 64)


@respx.mock
async def test_a_website_outage_warns_once_however_long_it_lasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The unattended processes used to exit on an outage; the container restarted,
    # failed again, and every boot sent the same warning — a Telegram message every
    # few seconds for as long as the site was down. Now: one warning, one recovery.
    _telegram_env(monkeypatch)
    attempts = 0

    async def flaky(settings: Settings) -> Profile:
        nonlocal attempts
        attempts += 1
        if attempts <= 5:
            raise ProfileUnavailableError("cannot read https://profile.test/en.md: 503")
        return _profile()

    async def no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr("project_pilot.cli._fetch_profile", flaky)
    sent = respx.post(_TG_SEND).respond(200, json={"ok": True})

    profile = await _wait_for_profile(Settings(), sleep=no_wait)

    assert profile.profile_hash == "h" * 64
    assert attempts == 6  # five outages, then the profile
    texts = [json.loads(call.request.content)["text"] for call in sent.calls]
    assert len(texts) == 2, texts
    assert "Profil nicht abrufbar" in texts[0]
    assert "Profil wieder erreichbar" in texts[1]


@respx.mock
async def test_a_healthy_start_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _telegram_env(monkeypatch)

    async def fine(settings: Settings) -> Profile:
        return _profile()

    monkeypatch.setattr("project_pilot.cli._fetch_profile", fine)
    sent = respx.post(_TG_SEND).respond(200, json={"ok": True})

    await _wait_for_profile(Settings())

    assert not sent.called


@respx.mock
def test_a_command_someone_typed_fails_on_the_terminal_not_on_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A human is watching the terminal already; a second copy of the error on the
    # phone is noise. No fallback to an older profile either.
    _telegram_env(monkeypatch)
    respx.get("https://profile.test/en.md").respond(503)
    sent = respx.post(_TG_SEND).respond(200, json={"ok": True})

    result = runner.invoke(app, ["run-once"])

    assert result.exit_code == 1
    assert "profile unavailable" in result.output
    assert not sent.called


def test_request_urls_are_kept_out_of_the_log() -> None:
    # The Telegram bot token sits inside the request URL, and httpx logs that URL
    # at INFO — so the secret would land in `docker compose logs`.
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.INFO)
    silence_request_logging()
    for name in ("httpx", "httpx2", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING


async def test_the_pipeline_builds_inside_a_running_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The daemon and run-once build the pipeline from *inside* asyncio.run. Loading
    # the profile with a nested asyncio.run there raised "cannot be called from a
    # running event loop" and put the deployed worker into a restart loop, while
    # every unit test stayed green because none built the pipeline in a loop.
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AAtest-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")

    pipeline, closer = await _build_pipeline(Settings(), _profile())
    try:
        assert pipeline is not None
    finally:
        await closer()

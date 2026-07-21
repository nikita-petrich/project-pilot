"""Tests for the Settings model and its fail-fast helpers."""

import pytest
from pydantic import ValidationError

from project_pilot.config import Settings, load_settings
from project_pilot.errors import ConfigError


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SCAN_INTERVAL_MIN", "ANALYSIS_WINDOW_MIN", "MATCH_THRESHOLD", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings()
    assert settings.scan_interval_min == 15
    assert settings.analysis_window_min == 30
    assert settings.match_threshold == 60
    assert settings.log_level == "info"


def test_scan_interval_below_minimum_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCAN_INTERVAL_MIN", "10")
    with pytest.raises(ValidationError):
        Settings()


def test_match_threshold_out_of_bounds_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATCH_THRESHOLD", "150")
    with pytest.raises(ValidationError):
        Settings()


def test_unknown_log_level_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    with pytest.raises(ValidationError):
        Settings()


def test_log_level_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    assert Settings().log_level == "warning"


def test_search_urls_parsed_from_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SEARCH_URLS",
        "https://a.example/x, https://b.example/y ,, https://c.example/z",
    )
    assert Settings().search_urls == [
        "https://a.example/x",
        "https://b.example/y",
        "https://c.example/z",
    ]


def test_user_agent_includes_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTACT_MAIL", "nik@example.com")
    user_agent = Settings().user_agent()
    assert "project-pilot/1.0" in user_agent
    assert "nik@example.com" in user_agent


def test_require_search_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_URLS", raising=False)
    with pytest.raises(ConfigError):
        Settings().require_search_urls()
    monkeypatch.setenv("SEARCH_URLS", "https://a.example/x")
    assert Settings().require_search_urls() == ["https://a.example/x"]


def test_require_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(ConfigError):
        Settings().require_telegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert Settings().require_telegram() == ("tok", "123")


def test_require_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(ConfigError):
        Settings().require_openai()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("LLM_MODEL", "gpt-mini")
    assert Settings().require_openai() == ("sk-x", "gpt-mini")


def test_load_settings_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCAN_INTERVAL_MIN", raising=False)
    assert isinstance(load_settings(), Settings)


def test_load_settings_wraps_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCAN_INTERVAL_MIN", "5")
    with pytest.raises(ConfigError):
        load_settings()

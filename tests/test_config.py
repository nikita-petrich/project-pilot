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


def test_require_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings()
    assert not settings.has_slack()
    with pytest.raises(ConfigError):
        settings.require_slack()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1")
    monkeypatch.setenv("SLACK_CHANNEL", "C0123")
    settings = Settings()
    assert settings.has_slack()
    slack = settings.require_slack()
    assert slack.bot_token == "xoxb-1"
    assert slack.app_token == "xapp-1"
    assert slack.channel == "C0123"


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


def _set_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_USER", "nik@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")


def test_require_smtp_rejects_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings()
    assert not settings.has_smtp()
    with pytest.raises(ConfigError):
        settings.require_smtp()


def test_require_smtp_defaults_sender_to_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_smtp(monkeypatch)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    smtp = Settings().require_smtp()
    assert smtp.host == "mail.example.com"
    assert smtp.port == 587
    assert smtp.sender == "nik@example.com"
    assert smtp.use_starttls is True


def test_require_smtp_honors_from_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_FROM", "bewerbung@nik.dev")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_STARTTLS", "false")
    smtp = Settings().require_smtp()
    assert smtp.sender == "bewerbung@nik.dev"
    assert smtp.port == 465
    assert smtp.use_starttls is False


def test_enrichment_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENRICHMENT_ENABLED", "ENRICHMENT_SEARCH", "ENRICHMENT_MAX_PAGES"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings()
    assert settings.enrichment_enabled is False
    assert settings.has_enrichment() is False
    assert settings.enrichment_search == "duckduckgo"
    assert settings.enrichment_max_pages == 6


def test_enrichment_enabled_and_search_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENRICHMENT_ENABLED", "true")
    monkeypatch.setenv("ENRICHMENT_SEARCH", "NONE")
    settings = Settings()
    assert settings.has_enrichment() is True
    assert settings.enrichment_search == "none"


def test_unknown_search_provider_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENRICHMENT_SEARCH", "bing")
    with pytest.raises(ValidationError):
        Settings()


def test_enrichment_render_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENRICHMENT_RENDER", "ENRICHMENT_RENDER_BROWSER_PATH", "APPLICANT_NAME"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings()
    assert settings.enrichment_render is False
    assert settings.enrichment_render_browser_path == ""
    assert settings.applicant_name == ""


def test_outreach_offer_du_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUTREACH_OFFER_DU", raising=False)
    assert Settings().outreach_offer_du is True
    monkeypatch.setenv("OUTREACH_OFFER_DU", "false")
    assert Settings().outreach_offer_du is False


def test_enrichment_render_and_applicant_name_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENRICHMENT_RENDER", "true")
    monkeypatch.setenv("APPLICANT_NAME", "Nik")
    settings = Settings()
    assert settings.enrichment_render is True
    assert settings.applicant_name == "Nik"


def test_enrichment_max_pages_out_of_bounds_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENRICHMENT_MAX_PAGES", "99")
    with pytest.raises(ValidationError):
        Settings()


def test_cv_attachments_default_to_the_repo_files(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    for name in ("CV_DE_PATH", "CV_EN_PATH", "CV_DE_DOCX_PATH", "CV_EN_DOCX_PATH"):
        monkeypatch.delenv(name, raising=False)
    cvs = Settings().cv_attachments()
    # Unset means "the CVs committed under cv/", so updating one is a file swap.
    assert cvs.de_pdf == Path("cv/CV-DE.pdf")
    assert cvs.en_pdf == Path("cv/CV-EN.pdf")
    assert cvs.de_docx == Path("cv/CV-DE-Word.docx")
    assert cvs.en_docx == Path("cv/CV-EN-Word.docx")


def test_cv_attachments_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CV_DE_PATH", "CV_EN_PATH", "CV_DE_DOCX_PATH", "CV_EN_DOCX_PATH"):
        monkeypatch.setenv(name, "")
    cvs = Settings().cv_attachments()
    assert cvs.de_pdf is None and cvs.en_pdf is None
    assert cvs.for_language("de") == []  # explicitly empty → nothing attached
    assert cvs.missing("de") == []  # and nothing reported as missing either


def test_cv_attachments_send_every_existing_file_language_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Every configured CV goes out; only the order follows the draft language."""
    from pathlib import Path

    base = Path(str(tmp_path))
    de_pdf, en_pdf, de_docx = base / "CV-DE.pdf", base / "CV-EN.pdf", base / "CV-DE-Word.docx"
    for path in (de_pdf, en_pdf, de_docx):
        path.write_bytes(b"%PDF")
    monkeypatch.setenv("CV_DE_PATH", str(de_pdf))
    monkeypatch.setenv("CV_EN_PATH", str(en_pdf))
    monkeypatch.setenv("CV_DE_DOCX_PATH", str(de_docx))
    monkeypatch.setenv("CV_EN_DOCX_PATH", str(base / "missing.docx"))
    cvs = Settings().cv_attachments()

    assert cvs.for_language("de") == [de_pdf, de_docx, en_pdf]
    assert cvs.for_language(None) == [de_pdf, de_docx, en_pdf]  # unknown → German first
    assert cvs.for_language("en") == [en_pdf, de_pdf, de_docx]
    # Configured but absent is reported rather than silently dropped.
    assert [path.name for path in cvs.missing("de")] == ["missing.docx"]

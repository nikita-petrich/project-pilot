"""Application configuration via pydantic-settings (parsed and validated at boot)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from project_pilot.errors import ConfigError

SOURCE_NAME = "freelancermap"
_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
_SEARCH_PROVIDERS = frozenset({"duckduckgo", "none"})


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """Validated SMTP connection settings for sending application e-mails."""

    host: str
    port: int
    username: str
    password: str
    sender: str
    use_starttls: bool


@dataclass(frozen=True, slots=True)
class SlackConfig:
    """Validated Slack settings: bot token (Web API), app token (Socket Mode), channel."""

    bot_token: str
    app_token: str
    channel: str


@dataclass(frozen=True, slots=True)
class CvAttachments:
    """The CV files attached to every application e-mail (PDF and Word, DE and EN).

    All configured CVs ride along on every send — a recruiter forwards whichever
    format and language their client wants. Only the order follows the draft
    language, so the matching CV is the first attachment.
    """

    de_pdf: Path | None
    en_pdf: Path | None
    de_docx: Path | None
    en_docx: Path | None

    def _ordered(self, language: str | None) -> tuple[Path | None, ...]:
        """Every configured path, the draft language's pair first."""
        german = (self.de_pdf, self.de_docx)
        english = (self.en_pdf, self.en_docx)
        return english + german if language == "en" else german + english

    def for_language(self, language: str | None) -> list[Path]:
        """The existing CV files to attach, matching-language first."""
        return [path for path in self._ordered(language) if path is not None and path.is_file()]

    def missing(self, language: str | None) -> list[Path]:
        """Configured CVs that are not on disk — surfaced so a gap never passes silently."""
        return [path for path in self._ordered(language) if path is not None and not path.is_file()]


class Settings(BaseSettings):
    """Typed view of the environment. Built once at CLI entry (fail fast).

    Secrets default to empty so the app stays importable and partially runnable
    without a full ``.env``; commands that need a secret enforce it through the
    ``require_*`` helpers rather than blocking every command at load time. The one
    always-on invariant is the compliance guardrail ``SCAN_INTERVAL_MIN >= 15``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://pilot:pilot@localhost:5432/project_pilot"
    contact_mail: str = "you@example.com"

    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_channel: str = ""

    openai_api_key: str = ""
    llm_model: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    # Default to the CVs versioned in the repo, so swapping a file in cv/ is the whole
    # update. The file name is what the recipient sees, hence the presentable casing.
    # A configured path that does not exist is skipped (and reported in the draft),
    # so the set can be filled in one file at a time.
    cv_de_path: str = "cv/CV-German.pdf"
    cv_en_path: str = "cv/CV-English.pdf"
    cv_de_docx_path: str = "cv/CV-German-Word.docx"
    cv_en_docx_path: str = "cv/CV-English-Word.docx"

    scan_interval_min: int = 15
    analysis_window_min: int = 30
    match_threshold: int = 60

    enrichment_enabled: bool = False
    enrichment_search: str = "duckduckgo"
    enrichment_max_pages: int = 6
    enrichment_render: bool = False
    enrichment_render_browser_path: str = ""
    outreach_offer_du: bool = True

    search_urls: Annotated[list[str], NoDecode] = Field(default_factory=list)
    log_level: str = "info"

    @field_validator("search_urls", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("scan_interval_min")
    @classmethod
    def _min_interval(cls, value: int) -> int:
        if value < 15:
            raise ValueError("SCAN_INTERVAL_MIN must be >= 15 (compliance guardrail)")
        return value

    @field_validator("analysis_window_min")
    @classmethod
    def _min_window(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ANALYSIS_WINDOW_MIN must be >= 1")
        return value

    @field_validator("match_threshold")
    @classmethod
    def _threshold_bounds(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("MATCH_THRESHOLD must be within 0..100")
        return value

    @field_validator("enrichment_search", mode="before")
    @classmethod
    def _known_search_provider(cls, value: object) -> object:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered not in _SEARCH_PROVIDERS:
                raise ValueError(f"ENRICHMENT_SEARCH must be one of {sorted(_SEARCH_PROVIDERS)}")
            return lowered
        return value

    @field_validator("enrichment_max_pages")
    @classmethod
    def _max_pages_bounds(cls, value: int) -> int:
        if not 1 <= value <= 20:
            raise ValueError("ENRICHMENT_MAX_PAGES must be within 1..20")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _known_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered not in _LOG_LEVELS:
                raise ValueError(f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}")
            return lowered
        return value

    def user_agent(self) -> str:
        """The identifying user agent required by the compliance guardrails."""
        return f"project-pilot/1.0 (personal project alert bot; contact: {self.contact_mail})"

    def require_search_urls(self) -> list[str]:
        if not self.search_urls:
            raise ConfigError("SEARCH_URLS is empty; set at least one search URL")
        return self.search_urls

    def has_slack(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token and self.slack_channel)

    def require_slack(self) -> SlackConfig:
        if not self.slack_bot_token:
            raise ConfigError("SLACK_BOT_TOKEN must be set (xoxb-… bot token)")
        if not self.slack_app_token:
            raise ConfigError("SLACK_APP_TOKEN must be set (xapp-… token for Socket Mode)")
        if not self.slack_channel:
            raise ConfigError("SLACK_CHANNEL must be set (channel id or name to post to)")
        return SlackConfig(
            bot_token=self.slack_bot_token,
            app_token=self.slack_app_token,
            channel=self.slack_channel,
        )

    def require_openai(self) -> tuple[str, str]:
        if not self.openai_api_key:
            raise ConfigError("OPENAI_API_KEY must be set")
        if not self.llm_model:
            raise ConfigError("LLM_MODEL must be set")
        return self.openai_api_key, self.llm_model

    def has_enrichment(self) -> bool:
        return self.enrichment_enabled

    def has_smtp(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    def require_smtp(self) -> SmtpConfig:
        if not self.has_smtp():
            raise ConfigError("SMTP_HOST, SMTP_USER and SMTP_PASSWORD must all be set")
        return SmtpConfig(
            host=self.smtp_host,
            port=self.smtp_port,
            username=self.smtp_user,
            password=self.smtp_password,
            sender=self.smtp_from or self.smtp_user,
            use_starttls=self.smtp_starttls,
        )

    def cv_attachments(self) -> CvAttachments:
        """Resolve the configured CV files (any of them may be unset)."""
        return CvAttachments(
            de_pdf=Path(self.cv_de_path) if self.cv_de_path else None,
            en_pdf=Path(self.cv_en_path) if self.cv_en_path else None,
            de_docx=Path(self.cv_de_docx_path) if self.cv_de_docx_path else None,
            en_docx=Path(self.cv_en_docx_path) if self.cv_en_docx_path else None,
        )


def load_settings() -> Settings:
    """Build ``Settings`` from the environment, converting failures to a clear abort."""
    try:
        return Settings()
    except ValidationError as err:
        raise ConfigError(f"invalid configuration: {err}") from err

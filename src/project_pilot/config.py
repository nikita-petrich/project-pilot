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
    """Optional CV files attached to application e-mails, chosen by draft language."""

    de: Path | None
    en: Path | None

    def for_language(self, language: str | None) -> Path | None:
        """The CV matching the draft language (English → EN, otherwise the German CV)."""
        chosen = self.en if language == "en" else self.de
        return chosen if chosen is not None and chosen.is_file() else None


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
    vision_model: str = ""  # optional override; falls back to LLM_MODEL

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    cv_de_path: str = ""
    cv_en_path: str = ""

    scan_interval_min: int = 15
    analysis_window_min: int = 30
    match_threshold: int = 60

    enrichment_enabled: bool = False
    enrichment_search: str = "duckduckgo"
    enrichment_max_pages: int = 6
    enrichment_render: bool = False
    enrichment_render_browser_path: str = ""
    applicant_name: str = ""
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

    def require_vision(self) -> tuple[str, str]:
        """API key plus the model that transcribes uploaded screenshots.

        ``VISION_MODEL`` only needs setting when ``LLM_MODEL`` has no image input.
        """
        api_key, model = self.require_openai()
        return api_key, self.vision_model or model

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
        """Resolve the configured CV files (either may be unset)."""
        return CvAttachments(
            de=Path(self.cv_de_path) if self.cv_de_path else None,
            en=Path(self.cv_en_path) if self.cv_en_path else None,
        )


def load_settings() -> Settings:
    """Build ``Settings`` from the environment, converting failures to a clear abort."""
    try:
        return Settings()
    except ValidationError as err:
        raise ConfigError(f"invalid configuration: {err}") from err

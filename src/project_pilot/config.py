"""Application configuration via pydantic-settings (parsed and validated at boot)."""

from typing import Annotated

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from project_pilot.errors import ConfigError

SOURCE_NAME = "freelancermap"
_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})


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

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    openai_api_key: str = ""
    llm_model: str = ""

    scan_interval_min: int = 15
    analysis_window_min: int = 30
    match_threshold: int = 60

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

    def require_telegram(self) -> tuple[str, str]:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set")
        return self.telegram_bot_token, self.telegram_chat_id

    def require_openai(self) -> tuple[str, str]:
        if not self.openai_api_key:
            raise ConfigError("OPENAI_API_KEY must be set")
        if not self.llm_model:
            raise ConfigError("LLM_MODEL must be set")
        return self.openai_api_key, self.llm_model


def load_settings() -> Settings:
    """Build ``Settings`` from the environment, converting failures to a clear abort."""
    try:
        return Settings()
    except ValidationError as err:
        raise ConfigError(f"invalid configuration: {err}") from err

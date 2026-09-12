"""Application configuration via pydantic-settings (parsed and validated at boot)."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from project_pilot.errors import ConfigError

logger = logging.getLogger(__name__)

SOURCE_NAME = "freelancermap"
_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
_SEARCH_PROVIDERS = frozenset({"duckduckgo", "none"})
# Anthropic's reasoning depths, plus "" for "do not send the parameter".
LlmEffort = Literal["", "low", "medium", "high", "xhigh", "max"]
_LLM_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_LLM_PROVIDERS = frozenset({"openai", "anthropic"})
# The ENV variable holding each provider's key, so an error names the one to set.
_LLM_KEY_VARS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


@dataclass(frozen=True, slots=True)
class LlmCredentials:
    """Which LLM the process talks to, resolved from the provider-specific settings.

    The provider decides only which SDK adapter is built; everything downstream of
    ``StructuredLlmClient``/``StructuredDraftClient`` is provider-agnostic.
    """

    provider: str
    api_key: str = field(repr=False)
    model: str
    # Reasoning depth, when the configured model takes one. Empty means "send
    # nothing", which is the only safe default: LLM_MODEL is free-form, and a
    # model that does not know the parameter rejects the whole request.
    effort: LlmEffort = ""


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """Validated SMTP connection settings for sending application e-mails."""

    host: str
    port: int
    username: str
    password: str = field(repr=False)  # keep the secret out of reprs/tracebacks
    sender: str
    use_starttls: bool


@dataclass(frozen=True, slots=True)
class CvAttachments:
    """The CV PDFs attached to every application e-mail (DE and EN).

    Both configured CVs ride along on every send — a recruiter forwards whichever
    language their client wants. Only the order follows the draft language, so the
    matching CV is the first attachment.
    """

    de_pdf: Path | None
    en_pdf: Path | None

    def _ordered(self, language: str | None) -> tuple[Path | None, ...]:
        """Both configured paths, the draft language's CV first."""
        return (self.en_pdf, self.de_pdf) if language == "en" else (self.de_pdf, self.en_pdf)

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

    # DATABASE_URL embeds the DB password, so keep it (and the secrets below) out of
    # any repr/traceback of Settings with repr=False.
    database_url: str = Field(
        default="postgresql+asyncpg://pilot:pilot@localhost:5432/project_pilot", repr=False
    )
    contact_mail: str = "you@example.com"

    # Which API the matcher and the draft generator call. Both adapters stay in the
    # code, so switching back is an ENV change and never a redeploy of new logic.
    llm_provider: str = "openai"
    openai_api_key: str = Field(default="", repr=False)
    anthropic_api_key: str = Field(default="", repr=False)
    llm_model: str = ""
    # Anthropic only (output_config.effort). OpenAI models take no such option here.
    llm_effort: LlmEffort = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = Field(default="", repr=False)
    smtp_from: str = ""
    smtp_starttls: bool = True

    # The CVs are pulled from a public Google Drive folder and cached under these
    # paths before each draft and send (see application/cv_drive.py), so swapping a
    # CV in Drive is the whole update — no commit, no redeploy. The file name is both
    # the Drive lookup key and what the recipient sees, hence the presentable casing.
    # Unset the folder id to fall back to plain local files. A CV that cannot be
    # fetched and has no cached copy is skipped and reported in the draft.
    cv_drive_folder_id: str = "1zfW069MqEkocmr8HXvnves4GI9xzXd0y"
    cv_de_path: str = "cv/CV-German.pdf"
    cv_en_path: str = "cv/CV-English.pdf"

    scan_interval_min: int = 15
    analysis_window_min: int = 30
    match_threshold: int = 60

    mcp_token: str = Field(default="", repr=False)
    mcp_port: int = 8765

    telegram_bot_token: str = Field(default="", repr=False)
    telegram_chat_id: str = ""

    # The page the Bewerben button opens with the card in its ``q`` parameter:
    # a new Claude chat by default; ``https://claude.ai/code/new`` for a Code
    # session instead (the documented fallback, see notification/claude_link.py).
    claude_session_url: str = "https://claude.ai/new"

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
        """Read a list from ENV the way a human writes one: ``a,b,c``.

        pydantic-settings would otherwise expect JSON for a list field and the
        process would die at boot with a type error.
        """
        if isinstance(value, str):
            # Tolerate the JSON form too, so a pasted ["a","b"] is not a boot error.
            items = (item.strip().strip("\"'") for item in value.strip().strip("[]").split(","))
            return [item for item in items if item]
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

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _known_llm_provider(cls, value: object) -> object:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered not in _LLM_PROVIDERS:
                raise ValueError(f"LLM_PROVIDER must be one of {sorted(_LLM_PROVIDERS)}")
            return lowered
        return value

    @field_validator("llm_effort", mode="before")
    @classmethod
    def _known_llm_effort(cls, value: object) -> object:
        """Reject a typo at boot rather than on the first real call.

        Empty is the default and means the parameter is not sent at all, which is
        what every model without a reasoning knob needs.
        """
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered and lowered not in _LLM_EFFORTS:
                raise ValueError(f"LLM_EFFORT must be empty or one of {sorted(_LLM_EFFORTS)}")
            return lowered
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

    def llm_api_key(self) -> str:
        """The configured key of the selected provider (empty when unset)."""
        return self.anthropic_api_key if self.llm_provider == "anthropic" else self.openai_api_key

    def require_llm(self) -> LlmCredentials:
        """The credentials the matcher and the draft generator need, or a clear abort."""
        api_key = self.llm_api_key()
        if not api_key:
            raise ConfigError(
                f"{_LLM_KEY_VARS[self.llm_provider]} must be set "
                f"(LLM_PROVIDER is '{self.llm_provider}')"
            )
        if not self.llm_model:
            raise ConfigError("LLM_MODEL must be set")
        return LlmCredentials(
            provider=self.llm_provider,
            api_key=api_key,
            model=self.llm_model,
            effort=self.llm_effort,
        )

    def require_telegram(self) -> tuple[str, str]:
        if not self.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN must be set (the bot token from @BotFather)")
        if not self.telegram_chat_id:
            raise ConfigError("TELEGRAM_CHAT_ID must be set (the chat the bot sends to)")
        return self.telegram_bot_token, self.telegram_chat_id

    def require_mcp(self) -> str:
        if not self.mcp_token:
            raise ConfigError("MCP_TOKEN must be set (bearer token for the MCP server)")
        return self.mcp_token

    def has_enrichment(self) -> bool:
        return self.enrichment_enabled

    def has_smtp(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    def require_smtp(self) -> SmtpConfig:
        if not self.has_smtp():
            raise ConfigError("SMTP_HOST, SMTP_USER and SMTP_PASSWORD must all be set")
        if not self.smtp_starttls and self.smtp_port != 465:
            # Port 465 is implicit TLS; on any other port STARTTLS is what encrypts
            # the session, so turning it off sends credentials and the e-mail in clear.
            logger.warning(
                "SMTP_STARTTLS is off on port %s: credentials and the application "
                "e-mail would be sent unencrypted",
                self.smtp_port,
            )
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
            de_pdf=Path(self.cv_de_path) if self.cv_de_path else None,
            en_pdf=Path(self.cv_en_path) if self.cv_en_path else None,
        )


def load_settings() -> Settings:
    """Build ``Settings`` from the environment, converting failures to a clear abort."""
    try:
        return Settings()
    except ValidationError as err:
        raise ConfigError(f"invalid configuration: {err}") from err

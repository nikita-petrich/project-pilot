"""Domain error types and the assert_defined null-safety helper."""


class ProjectPilotError(Exception):
    """Base class for all project-pilot domain errors."""


class ConfigError(ProjectPilotError):
    """Configuration or profile is missing or invalid (fail fast at boot)."""


class SourceBlockedError(ProjectPilotError):
    """The source returned 403 or a captcha/bot wall; back off, never retry."""


class SelectorMismatchError(ProjectPilotError):
    """A parser selector matched nothing; the source markup likely changed."""


class LlmSchemaError(ProjectPilotError):
    """The LLM response did not satisfy the expected schema."""


class EmailSendError(ProjectPilotError):
    """The SMTP delivery of an application e-mail failed."""


class ApplicationStateError(ProjectPilotError):
    """An application action is not allowed in its current state (guarded flow)."""


class EnrichmentError(ProjectPilotError):
    """Contact enrichment could not run (nothing to look up, or search disabled)."""


def assert_defined[T](value: T | None, msg: str) -> T:
    """Return ``value`` when it is not ``None``, else raise ``ProjectPilotError``.

    Preferred over a bare ``assert`` (which strips under ``-O``) or a scattered
    ``# type: ignore``: it narrows ``T | None`` to ``T`` for the type checker and
    fails loudly with a meaningful message at runtime.
    """
    if value is None:
        raise ProjectPilotError(msg)
    return value

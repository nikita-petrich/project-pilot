"""Structured LLM output schema for application drafts."""

from pydantic import BaseModel, field_validator

LINKEDIN_LIMIT = 250


class ApplicationDraft(BaseModel):
    """The structured application the LLM must return (used as OpenAI response format)."""

    subject: str
    body: str
    linkedin_message: str

    @field_validator("subject", "body", "linkedin_message")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("subject")
    @classmethod
    def _single_line_subject(cls, value: str) -> str:
        """A mail header must be one line; also stay inside the DB column (512)."""
        return " ".join(value.split())[:500]

    @field_validator("linkedin_message")
    @classmethod
    def _cap_linkedin(cls, value: str) -> str:
        if len(value) <= LINKEDIN_LIMIT:
            return value
        return value[: LINKEDIN_LIMIT - 1].rstrip() + "…"

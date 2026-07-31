"""Structured LLM output schema for application drafts."""

from pydantic import BaseModel, field_validator

LINKEDIN_LIMIT = 250
TITLE_LIMIT = 120


class ApplicationDraft(BaseModel):
    """The structured application the LLM must return (used as OpenAI response format).

    ``project_title`` is the listing's headline (copied, or written by the model when
    the description has none) — it names the draft in Slack instead of the first
    line of a pasted recruiter mail.
    """

    project_title: str
    subject: str
    body: str
    linkedin_message: str

    @field_validator("project_title", "subject", "body", "linkedin_message")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("project_title")
    @classmethod
    def _single_line_title(cls, value: str) -> str:
        """One line, short enough for a Slack header and the DB column (512)."""
        return " ".join(value.split())[:TITLE_LIMIT]

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

"""Structured LLM output schema for application drafts."""

from pydantic import BaseModel, field_validator

from project_pilot.application.linkedin import fit_linkedin_message

# LinkedIn's own cap for a connection-request note; the message must carry the
# booking link plus the phone alternative, so the full budget is used.
LINKEDIN_LIMIT = 300
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
        """Keep the note inside LinkedIn's limit without cutting off its ending.

        The closing call to action carries the booking link and the phone number, so
        an over-long note loses whole sentences from its middle instead of its tail.
        """
        return fit_linkedin_message(value, LINKEDIN_LIMIT)

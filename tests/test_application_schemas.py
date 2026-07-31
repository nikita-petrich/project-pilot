"""Tests for the ApplicationDraft schema (whitespace, title, LinkedIn cap)."""

from project_pilot.application.schemas import LINKEDIN_LIMIT, TITLE_LIMIT, ApplicationDraft


def test_fields_are_stripped() -> None:
    draft = ApplicationDraft(
        project_title="  T  ", subject="  S  ", body="  B  ", linkedin_message="  L  "
    )
    assert draft.project_title == "T"
    assert draft.subject == "S"
    assert draft.body == "B"
    assert draft.linkedin_message == "L"


def test_linkedin_message_capped_at_limit() -> None:
    draft = ApplicationDraft(project_title="t", subject="s", body="b", linkedin_message="x" * 400)
    assert len(draft.linkedin_message) <= LINKEDIN_LIMIT
    assert draft.linkedin_message.endswith("…")


def test_linkedin_message_within_limit_untouched() -> None:
    message = "Kurz und gut."
    draft = ApplicationDraft(project_title="t", subject="s", body="b", linkedin_message=message)
    assert draft.linkedin_message == message


def test_subject_collapsed_to_single_line_and_capped() -> None:
    draft = ApplicationDraft(
        project_title="t", subject="Zeile 1\nZeile 2", body="b", linkedin_message="l"
    )
    assert draft.subject == "Zeile 1 Zeile 2"
    long = ApplicationDraft(project_title="t", subject="s" * 600, body="b", linkedin_message="l")
    assert len(long.subject) == 500


def test_project_title_collapsed_to_single_line_and_capped() -> None:
    """The title names a Slack header, so it must stay one short line."""
    draft = ApplicationDraft(
        project_title="Senior Fullstack\nTypeScript Developer",
        subject="s",
        body="b",
        linkedin_message="l",
    )
    assert draft.project_title == "Senior Fullstack TypeScript Developer"
    long = ApplicationDraft(project_title="t" * 300, subject="s", body="b", linkedin_message="l")
    assert len(long.project_title) == TITLE_LIMIT

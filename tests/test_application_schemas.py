"""Tests for the ApplicationDraft schema (whitespace, LinkedIn cap)."""

from project_pilot.application.schemas import LINKEDIN_LIMIT, ApplicationDraft


def test_fields_are_stripped() -> None:
    draft = ApplicationDraft(subject="  S  ", body="  B  ", linkedin_message="  L  ")
    assert draft.subject == "S"
    assert draft.body == "B"
    assert draft.linkedin_message == "L"


def test_linkedin_message_capped_at_limit() -> None:
    draft = ApplicationDraft(subject="s", body="b", linkedin_message="x" * 400)
    assert len(draft.linkedin_message) <= LINKEDIN_LIMIT
    assert draft.linkedin_message.endswith("…")


def test_linkedin_message_within_limit_untouched() -> None:
    message = "Kurz und gut."
    draft = ApplicationDraft(subject="s", body="b", linkedin_message=message)
    assert draft.linkedin_message == message


def test_subject_collapsed_to_single_line_and_capped() -> None:
    draft = ApplicationDraft(subject="Zeile 1\nZeile 2", body="b", linkedin_message="l")
    assert draft.subject == "Zeile 1 Zeile 2"
    long = ApplicationDraft(subject="s" * 600, body="b", linkedin_message="l")
    assert len(long.subject) == 500

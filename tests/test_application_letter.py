"""Letter form fixed after generation: what the model gets right only most of the time."""

import re

from project_pilot.application.generator import load_application_prompt
from project_pilot.application.letter import (
    CONFIDENTIALITY_NOTICES,
    lowercase_after_salutation,
    tidy_body,
    unwrap_confidentiality_notice,
)
from project_pilot.application.schemas import ApplicationDraft

# Verbatim from a real draft (application 46, 2026-09-14): capital "Ich" after the
# salutation, and the notice hard-wrapped mid-sentence.
_REAL_OPENING = "Guten Tag Talissa Blajan,\n\nIch bin seit über 7 Jahren als freiberuflicher"
_REAL_NOTICE = """Der Inhalt dieser E-Mail ist ausschließlich für den bezeichneten Adressaten
bestimmt. Wenn Sie nicht der vorgesehene Adressat dieser E-Mail oder dessen
Vertreter sein sollten, so beachten Sie bitte, dass jede Form der Kenntnisnahme,
Veröffentlichung, Vervielfältigung oder Weitergabe des Inhalts dieser E-Mail
unzulässig ist. Wir bitten Sie, sich in diesem Fall mit dem Absender der E-Mail
in Verbindung zu setzen."""


def test_the_sentence_after_a_comma_salutation_continues_in_lower_case() -> None:
    assert lowercase_after_salutation(_REAL_OPENING).startswith(
        "Guten Tag Talissa Blajan,\n\nich bin seit"
    )


def test_only_the_pronoun_is_touched() -> None:
    # A noun may open the text ("Ihre Ausschreibung"), and English keeps its "I".
    for body in (
        "Sehr geehrte Damen und Herren,\n\nIhre Ausschreibung hat mich sofort angesprochen.",
        "Dear Talissa Blajan,\n\nI have been working as a Senior Engineer",
        "Guten Tag,\n\nich bin bereits richtig klein geschrieben.",
    ):
        assert lowercase_after_salutation(body) == body
    # "Ich" deeper in the text is a sentence start and stays capitalised.
    body = "Guten Tag Max Muster,\n\nich bin da.\n\nIch freue mich."
    assert lowercase_after_salutation(body) == body


def test_a_wrapped_notice_becomes_one_paragraph() -> None:
    body = f"Text.\n\nUSt-IdNr.: DE368159064\n\n{_REAL_NOTICE}"
    tidy = unwrap_confidentiality_notice(body)
    assert tidy.endswith(CONFIDENTIALITY_NOTICES[0])
    assert tidy.startswith("Text.\n\nUSt-IdNr.: DE368159064\n\n")


def test_the_generated_draft_arrives_already_corrected() -> None:
    draft = ApplicationDraft(
        project_title="Senior Software Engineer",
        subject="Bewerbung",
        body=f"{_REAL_OPENING} Engineer tätig.\n\n-- \nViele Grüße\n\n{_REAL_NOTICE}",
        linkedin_message="Guten Tag Talissa Blajan, …",
    )
    assert "\n\nich bin seit" in draft.body
    assert draft.body.endswith(CONFIDENTIALITY_NOTICES[0])
    assert "\n-- \n" in draft.body  # the signature separator keeps its trailing space
    assert tidy_body(draft.body) == draft.body  # idempotent


def test_the_notice_in_code_is_the_notice_the_prompt_demands() -> None:
    # Two copies of one legal text: this keeps them identical, word for word.
    prompt = " ".join(load_application_prompt().split())
    for notice in CONFIDENTIALITY_NOTICES:
        assert notice in prompt, notice[:40]
    assert re.search(r"Der Inhalt dieser E-Mail", prompt)

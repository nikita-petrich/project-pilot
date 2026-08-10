"""Tests for fitting an over-long LinkedIn note into the connection-note limit."""

from project_pilot.application.linkedin import fit_linkedin_message

_LIMIT = 300
_CTA = (
    "Kostenloses Erstgespräch: https://calendar.notion.so/meet/petrichnikita/"
    "erstgespraech-30-min — oder rufen Sie mich direkt an: +49 1567 9088678."
)


def test_short_message_is_returned_unchanged() -> None:
    assert fit_linkedin_message("Guten Tag Frau Meier, gerne vernetzen.", _LIMIT) == (
        "Guten Tag Frau Meier, gerne vernetzen."
    )


def test_overlong_message_drops_the_middle_and_keeps_link_and_phone() -> None:
    """The ending carries the booking link and the phone number — it must survive."""
    message = (
        "Guten Tag Frau Meier, zu Ihrer Ausschreibung Senior AI Engineer. "
        "Ich passe mit Python, LLM-Integration, RAG-Architekturen und produktiver "
        "Cloud-Erfahrung sehr gut auf die beschriebene Rolle. "
        "Zusätzlich bringe ich Erfahrung aus LegalTech und Plattformkonsolidierungen mit. "
        f"{_CTA}"
    )
    assert len(message) > _LIMIT
    fitted = fit_linkedin_message(message, _LIMIT)
    assert len(fitted) <= _LIMIT
    assert fitted.startswith("Guten Tag Frau Meier, zu Ihrer Ausschreibung")
    assert fitted.endswith(_CTA)  # link and phone number intact, nothing cut mid-token
    assert "…" not in fitted


def test_falls_back_to_trimming_the_opening_when_two_sentences_still_overflow() -> None:
    message = f"{'Guten Tag Frau Meier, ' * 12}zu Ihrer Ausschreibung. {_CTA}"
    fitted = fit_linkedin_message(message, _LIMIT)
    assert len(fitted) <= _LIMIT
    assert fitted.endswith(_CTA)
    assert fitted.startswith("Guten Tag Frau Meier,")
    assert "…" in fitted  # the opening was shortened, visibly


def test_a_single_overlong_sentence_is_cut_on_a_word_boundary() -> None:
    fitted = fit_linkedin_message("wort " * 200, _LIMIT)
    assert len(fitted) <= _LIMIT
    assert fitted.endswith("…")
    assert "wor…" not in fitted  # never mid-word


def test_an_unbreakable_run_is_still_bounded() -> None:
    fitted = fit_linkedin_message("x" * 500, _LIMIT)
    assert len(fitted) <= _LIMIT

"""Provenance on a contact datum, and reading the stored form back."""

from project_pilot.enrichment.schemas import ContactDatum, contact_data


def test_a_datum_defaults_to_own_research() -> None:
    # "web" is the honest default: anything not explicitly stated by the agent on a
    # board page was found by us and wants checking before it is written to.
    assert ContactDatum("info@firma.de").source == "web"
    assert ContactDatum("info@firma.de").as_json() == {
        "value": "info@firma.de",
        "source": "web",
    }


def test_rows_written_before_provenance_read_back_as_web() -> None:
    # contact_leads holds JSONB, so the old rows are plain strings. They are what
    # they always were — own research — not an unknown to guess about.
    assert contact_data(["a@firma.de", "b@firma.de"]) == [
        ContactDatum("a@firma.de", "web"),
        ContactDatum("b@firma.de", "web"),
    ]


def test_new_rows_keep_the_source_they_were_written_with() -> None:
    stored = [
        {"value": "i.strucken@weissenberg.de", "source": "freelancermap"},
        {"value": "info@weissenberg.de", "source": "web"},
    ]
    assert contact_data(stored) == [
        ContactDatum("i.strucken@weissenberg.de", "freelancermap"),
        ContactDatum("info@weissenberg.de", "web"),
    ]


def test_unusable_entries_are_skipped_rather_than_guessed_at() -> None:
    # An unknown source label is not trusted into "freelancermap"; a shapeless entry
    # is dropped instead of becoming a datum nobody can account for.
    assert contact_data(
        [
            "",
            None,
            42,
            {"source": "freelancermap"},
            {"value": "x@firma.de", "source": "carrier pigeon"},
        ]
    ) == [ContactDatum("x@firma.de", "web")]


def test_a_missing_column_is_an_empty_list_not_a_crash() -> None:
    assert contact_data(None) == []
    assert contact_data("info@firma.de") == []

"""Tests for the LinkedIn connection-message builder."""

from project_pilot.enrichment.message import LINKEDIN_CONNECT_LIMIT, build_connection_message


def test_message_personalizes_from_person_company_and_title() -> None:
    msg = build_connection_message(
        person="Max Mustermann", company="Muster GmbH", title="Data Engineer"
    )
    assert msg.startswith("Hallo Max,")
    assert "Muster GmbH" in msg
    assert "Data Engineer" in msg
    assert "vernetzen" in msg
    assert len(msg) <= LINKEDIN_CONNECT_LIMIT


def test_message_strips_titles_from_first_name() -> None:
    msg = build_connection_message(person="Dr. Anna Schmidt", company="X")
    assert msg.startswith("Hallo Anna,")


def test_message_signs_with_sender() -> None:
    msg = build_connection_message(person="Max Mustermann", company="X", sender="Nik")
    assert msg.rstrip().endswith("Nik")


def test_message_from_company_only() -> None:
    msg = build_connection_message(company="Muster GmbH")
    assert msg.startswith("Hallo,")
    assert "Muster GmbH" in msg
    assert len(msg) <= LINKEDIN_CONNECT_LIMIT


def test_message_is_always_produced_even_with_nothing() -> None:
    msg = build_connection_message()
    assert msg.startswith("Hallo,") and "vernetzen" in msg


def test_message_caps_a_very_long_title_at_the_limit() -> None:
    msg = build_connection_message(
        person="Max Mustermann", company="Muster GmbH", title="Sehr langer Projekttitel " * 30
    )
    assert len(msg) <= LINKEDIN_CONNECT_LIMIT
    assert "vernetzen" in msg  # the ask survives; the title is what gets shortened

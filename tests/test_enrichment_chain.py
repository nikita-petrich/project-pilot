"""The fallback chain on its own: which contact answers, with nothing fetched."""

from project_pilot.enrichment.chain import resolve_emails, resolve_persons, resolve_phones
from project_pilot.enrichment.schemas import ContactDatum

_IMPRESSUM = "info@weissenberg.de"
_PERSONAL = "i.strucken@weissenberg.de"
_STATED = "kontakt@weissenberg.de"


def _pairs(data: list[ContactDatum]) -> list[tuple[str, str]]:
    return [(datum.value, datum.source) for datum in data]


def test_the_ad_names_someone_and_the_company_page_answers() -> None:
    # Top of the chain: what the agent put on their own page wins outright, even
    # over an address that carries the named person's name.
    resolved = resolve_emails(
        stated=[_STATED], found=[_PERSONAL, _IMPRESSUM], person="Ines Strucken"
    )
    assert _pairs(resolved)[0] == (_STATED, "freelancermap")


def test_the_ad_names_someone_and_the_company_page_is_silent() -> None:
    # Second tier: the address carrying the person's name beats the role mailbox.
    resolved = resolve_emails(stated=[], found=[_IMPRESSUM, _PERSONAL], person="Ines Strucken")
    assert _pairs(resolved) == [(_PERSONAL, "web"), (_IMPRESSUM, "web")]


def test_the_ad_names_nobody_so_the_chain_goes_straight_to_the_impressum() -> None:
    # No person means no personal tier: hunting for "somebody's" address when the ad
    # names nobody is guessing, and the role mailbox is the honest answer.
    resolved = resolve_emails(stated=[], found=[_PERSONAL, _IMPRESSUM], person=None)
    assert _pairs(resolved)[0] == (_IMPRESSUM, "web")


def test_the_ad_names_nobody_but_the_company_page_does() -> None:
    resolved = resolve_emails(stated=[_STATED], found=[_IMPRESSUM], person=None)
    assert _pairs(resolved) == [(_STATED, "freelancermap"), (_IMPRESSUM, "web")]


def test_a_value_found_in_both_places_keeps_the_stronger_provenance() -> None:
    # Otherwise the same address would read as "we found this somewhere" purely
    # because the crawl happened to list it first.
    resolved = resolve_emails(stated=[_IMPRESSUM], found=[_IMPRESSUM], person=None)
    assert _pairs(resolved) == [(_IMPRESSUM, "freelancermap")]


def test_phones_follow_the_same_order_without_a_person_tier() -> None:
    # A switchboard number carries no name to match, so there are only two tiers.
    resolved = resolve_phones(stated=["+4921154080932"], found=["+49301234567"])
    assert _pairs(resolved) == [
        ("+4921154080932", "freelancermap"),
        ("+49301234567", "web"),
    ]


def test_nothing_anywhere_is_an_empty_chain_not_a_placeholder() -> None:
    assert resolve_emails(stated=[], found=[], person="Ines Strucken") == []
    assert resolve_phones(stated=[], found=[]) == []
    assert resolve_persons(stated=[], found=[]) == []

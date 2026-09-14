"""The fallback chain on its own: which contact answers, with nothing fetched."""

from project_pilot.enrichment.chain import resolve_emails, resolve_persons, resolve_phones
from project_pilot.enrichment.schemas import ContactDatum

_IMPRESSUM = "info@weissenberg.de"
_PERSONAL = "i.strucken@weissenberg.de"
_STATED = "kontakt@weissenberg.de"


def _pairs(data: list[ContactDatum]) -> list[tuple[str, str]]:
    return [(datum.value, datum.source) for datum in data]


def test_the_contact_persons_own_address_comes_first_wherever_it_was_found() -> None:
    # The application is addressed to the person the ad names, so their own address
    # beats the company page — even when only the crawl found it.
    resolved = resolve_emails(
        stated=[_STATED], found=[_IMPRESSUM, _PERSONAL], person="Ines Strucken"
    )
    assert _pairs(resolved) == [
        (_PERSONAL, "web"),
        (_STATED, "freelancermap"),
        (_IMPRESSUM, "web"),
    ]


def test_without_the_contact_persons_address_the_company_page_answers() -> None:
    # The Randstad case: the ad names Talissa Blajan, the only address belongs to a
    # colleague on the company page. It goes there; the agency forwards internally.
    resolved = resolve_emails(
        stated=["laura.rossmeier@randstaddigital.com"],
        found=["info@randstaddigital.com"],
        person="Talissa Blajan",
    )
    assert _pairs(resolved) == [
        ("laura.rossmeier@randstaddigital.com", "freelancermap"),
        ("info@randstaddigital.com", "web"),
    ]


def test_a_personal_address_on_the_company_page_keeps_its_stated_label() -> None:
    resolved = resolve_emails(stated=[_PERSONAL], found=[_PERSONAL], person="Ines Strucken")
    assert _pairs(resolved) == [(_PERSONAL, "freelancermap")]


def test_the_ad_names_someone_and_the_company_page_is_silent() -> None:
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

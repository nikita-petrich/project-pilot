"""The fallback chain: which contact answers, and in what order.

This is the rule on its own, with no fetching anywhere near it, because the rule
is the part worth being sure about:

============================  ==================================================
The ad names a contact person  the board's company page, then an address carrying
                               that person's name, then the Impressum mailbox
The ad names nobody            the board's company page, then straight to the
                               Impressum mailbox — no personal address is hunted
                               for, because there is no person to hunt for
============================  ==================================================

The same shape holds for the phone number, minus the person tier: a switchboard
number carries no name to match against.

Two principles run through it. What the agent stated on their own company page
outranks anything a crawl turned up, because they maintain it themselves. And a
value found in both places keeps the provenance that says so, rather than the one
that happens to be discovered first.
"""

from project_pilot.enrichment.extract import rank_emails
from project_pilot.enrichment.schemas import FREELANCERMAP, WEB, ContactDatum, ContactSource


def _mark(values: list[str], source: ContactSource) -> list[ContactDatum]:
    return [ContactDatum(value, source) for value in values]


def _merge(*groups: list[ContactDatum]) -> list[ContactDatum]:
    """Concatenate the groups, most-trusted first, keeping one entry per value."""
    seen: dict[str, ContactDatum] = {}
    for group in groups:
        for datum in group:
            seen.setdefault(datum.value, datum)
    return list(seen.values())


def resolve_emails(
    *, stated: list[str], found: list[str], person: str | None
) -> list[ContactDatum]:
    """Order the addresses so the head of the list is the one to write to.

    ``person`` is the contact the ad itself names, or ``None`` when it names
    nobody — which is what turns the personal tier off and sends the chain
    straight to the role mailbox.
    """
    return _merge(
        _mark(rank_emails(stated, person), FREELANCERMAP),
        _mark(rank_emails(found, person), WEB),
    )


def resolve_phones(*, stated: list[str], found: list[str]) -> list[ContactDatum]:
    """The same order for numbers: what the agent stated, then what we found."""
    return _merge(_mark(stated, FREELANCERMAP), _mark(found, WEB))


def resolve_persons(*, stated: list[str], found: list[str]) -> list[ContactDatum]:
    """Names encountered along the way, stated ones first. Display only."""
    return _merge(_mark(stated, FREELANCERMAP), _mark(found, WEB))

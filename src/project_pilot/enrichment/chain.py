"""The fallback chain: which contact answers, and in what order.

This is the rule on its own, with no fetching anywhere near it, because the rule
is the part worth being sure about:

============================  ==================================================
The ad names a contact person  an address carrying that person's name, wherever
                               it was found; then the board's company page; then
                               whatever else the crawl turned up
The ad names nobody            the board's company page, then the crawl's finds —
                               no personal address is hunted for, because there
                               is no person to hunt for
============================  ==================================================

The contact person's own address comes first because that is who the application
is addressed to. Without one, the company page is next: the agency maintains it
itself, and an agency forwards a mail to the named contact internally — so the
salutation stays with the person from the ad even when the address is someone
else's (pattern L1 in blueprint/context/learnings.md).

The same shape holds for the phone number, minus the person tier: a switchboard
number carries no name to match against. And a value found in two places keeps the
provenance that says the agency stated it, rather than the one discovered first.
"""

from project_pilot.enrichment.extract import belongs_to, rank_emails
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
    stated_ranked = rank_emails(stated, person)
    found_ranked = rank_emails(found, person)
    return _merge(
        # 1. the contact person's own address — stated before found, so a value in
        #    both keeps its "freelancermap" label
        _mark([email for email in stated_ranked if belongs_to(email, person)], FREELANCERMAP),
        _mark([email for email in found_ranked if belongs_to(email, person)], WEB),
        # 2. the company page, 3. everything else the crawl found
        _mark(stated_ranked, FREELANCERMAP),
        _mark(found_ranked, WEB),
    )


def resolve_phones(*, stated: list[str], found: list[str]) -> list[ContactDatum]:
    """The same order for numbers: what the agent stated, then what we found."""
    return _merge(_mark(stated, FREELANCERMAP), _mark(found, WEB))


def resolve_persons(*, stated: list[str], found: list[str]) -> list[ContactDatum]:
    """Names encountered along the way, stated ones first. Display only."""
    return _merge(_mark(stated, FREELANCERMAP), _mark(found, WEB))

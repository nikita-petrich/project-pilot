"""The research table: contact data and research links in one narrow block.

A chat prints it as it comes. Two columns and short link texts keep it narrow
enough to read without scrolling sideways — a bare LinkedIn search URL is a
hundred characters, so every link carries a name instead (the person, the
website's host) and the URL stays behind it.

Rendered here rather than left to the chat, because the chat laid the same data
out differently every time (learnings L5: what is mechanical is done in code).
"""

from collections.abc import Sequence
from urllib.parse import urlsplit

from project_pilot.enrichment.links import (
    google_search_url,
    linkedin_company_url,
    linkedin_people_url,
)
from project_pilot.enrichment.schemas import ContactDatum, ContactEnrichment
from project_pilot.notification.messages import NO_CONTACT, MatchMessage

Row = tuple[str, str]

# Where a value came from when it was read off the listing itself.
LISTING = "Inserat"


def _cell(text: str) -> str:
    """A table cell: a pipe or a line break would split the row."""
    return " ".join(text.replace("|", "\\|").split())


def _link(text: str, url: str) -> str:
    label = text.replace("[", "(").replace("]", ")")
    return f"[{label}]({url.replace(' ', '%20').replace(')', '%29')})"


def _host(url: str) -> str:
    host = urlsplit(url if "://" in url else f"https://{url}").netloc or url
    return host.removeprefix("www.")


def _sourced(datum: ContactDatum) -> str:
    return f"{datum.value} · {datum.source}"


def table(rows: Sequence[Row]) -> str:
    """Two-column markdown table; empty values are left out, not shown blank."""
    lines = ["| Angabe | Wert |", "|---|---|"]
    lines.extend(f"| {_cell(label)} | {_cell(value)} |" for label, value in rows if value)
    return "\n".join(lines)


def research_rows(message: MatchMessage) -> list[Row]:
    """The card's database coordinates and research links, one row each."""
    rows: list[Row] = []
    if message.listing_id is not None:
        parts = [
            str(message.listing_id),
            *(part for part in (message.source, message.origin, message.status) if part),
        ]
        rows.append(("DB", " · ".join(parts)))
    if message.threshold is not None:
        reached = "erreicht" if message.score >= message.threshold else "verfehlt"
        rows.append(("Schwelle", f"{message.threshold} ({reached})"))
    if message.posted_at_label:
        rows.append(("Eingestellt", f"{message.posted_at_label} (Berlin)"))
    if message.onsite_only:
        rows.append(("Hinweis", "🚨 reines Vor-Ort-Projekt — vor dem Senden prüfen"))
    if message.company_page:
        rows.append(("Firmenseite", _link(_host(message.company_page), message.company_page)))
    if message.company:
        rows.append(
            ("LinkedIn Firma", _link(message.company, linkedin_company_url(message.company)))
        )
    rows.append(("LinkedIn Person", _person_search(message)))
    if message.company:
        query = google_search_url(f"{message.company} Impressum Kontakt E-Mail")
        rows.append(("Impressum", _link("Google-Suche", query)))
    return rows


def _person_search(message: MatchMessage) -> str:
    if not message.contact_name:
        return NO_CONTACT
    url = linkedin_people_url(company=message.company, person=message.contact_name)
    return _link(message.contact_name, url)


def research_table(message: MatchMessage) -> str:
    """The research links on their own, for a card shown without contact research."""
    return table(research_rows(message))


def overview_table(message: MatchMessage, contacts: ContactEnrichment) -> str:
    """Contact data first, then the research links and coordinates — one table.

    Every contact value names its source (``Inserat``, ``freelancermap``,
    ``web``); the order of the e-mails is the resolved chain's, best first.
    """
    company = message.company or contacts.company
    rows: list[Row] = []
    if company:
        rows.append(("Firma", f"{company} · {LISTING}" if message.company else company))
    if message.contact_name:
        rows.append(("Ansprechperson", f"{message.contact_name} · {LISTING}"))
    rows.extend(("E-Mail", _sourced(datum)) for datum in contacts.emails)
    if not contacts.emails:
        rows.append(("E-Mail", "keine gefunden"))
    rows.extend(("Telefon", _sourced(datum)) for datum in contacts.phones)
    rows.extend(
        ("Person", _sourced(datum))
        for datum in contacts.persons
        if datum.value.casefold() != (message.contact_name or "").casefold()
    )
    if contacts.website:
        rows.append(("Website", _link(_host(contacts.website), contacts.website)))
    rows.extend(research_rows(message))
    return table(rows)

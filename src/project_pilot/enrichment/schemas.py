"""Result shapes for contact enrichment (pure data, no I/O)."""

from dataclasses import dataclass, field
from typing import Literal

# Where a datum came from. The distinction is the point of carrying it: an address
# the agent filled in on their own freelancermap company page is a stated contact,
# while one the Impressum crawl picked up is a find that has to be looked at before
# anything is sent to it.
type ContactSource = Literal["freelancermap", "web"]

FREELANCERMAP: ContactSource = "freelancermap"
WEB: ContactSource = "web"


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One web-search hit: the target URL and its human-readable title."""

    url: str
    title: str


@dataclass(frozen=True, slots=True)
class ContactDatum:
    """One e-mail, phone number or person's name, plus where it was found."""

    value: str
    source: ContactSource = WEB

    def as_json(self) -> dict[str, str]:
        """The JSONB shape stored in ``contact_leads`` and returned over MCP."""
        return {"value": self.value, "source": self.source}


def contact_data(values: object) -> list[ContactDatum]:
    """Read a stored ``contact_leads`` list back, whatever shape it was written in.

    Rows written before provenance existed hold plain strings; they count as
    ``web``, which is what they were. Anything else in the list is skipped rather
    than guessed at.
    """
    if not isinstance(values, list):
        return []
    data: list[ContactDatum] = []
    for entry in values:
        if isinstance(entry, str) and entry:
            data.append(ContactDatum(entry))
        elif isinstance(entry, dict):
            value, source = entry.get("value"), entry.get("source")
            if isinstance(value, str) and value:
                data.append(ContactDatum(value, FREELANCERMAP if source == FREELANCERMAP else WEB))
    return data


@dataclass(frozen=True, slots=True)
class DiscoveryLinks:
    """Ready-to-click research links.

    These are *constructed* search URLs, never scraped: Nik opens them in his own
    authenticated browser session. This is how "search the company/person on
    LinkedIn" and "search on Google" are honored without violating either site's
    terms or fetching their pages.
    """

    linkedin_company: str
    linkedin_people: str
    google_company: str
    google_contact: str


@dataclass(frozen=True, slots=True)
class ContactEnrichment:
    """Everything found for one company/person lookup, best candidates first."""

    company: str | None
    person: str | None
    website: str | None
    links: DiscoveryLinks
    linkedin_message: str = ""
    emails: list[ContactDatum] = field(default_factory=list)
    phones: list[ContactDatum] = field(default_factory=list)
    persons: list[ContactDatum] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    # The company's own page on the board the listing came from, when the listing
    # named one. Read off the listing, never constructed from the company name.
    company_page: str | None = None

    @property
    def best_email(self) -> ContactDatum | None:
        """The head of the resolved chain — with its provenance, which decides trust."""
        return self.emails[0] if self.emails else None

    @property
    def best_phone(self) -> ContactDatum | None:
        return self.phones[0] if self.phones else None

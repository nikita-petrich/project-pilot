"""Result shapes for contact enrichment (pure data, no I/O)."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One web-search hit: the target URL and its human-readable title."""

    url: str
    title: str


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
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    persons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def best_email(self) -> str | None:
        """The top-ranked e-mail candidate (persons/role addresses rank first)."""
        return self.emails[0] if self.emails else None

    @property
    def best_phone(self) -> str | None:
        return self.phones[0] if self.phones else None

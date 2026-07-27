"""Database-aware enrichment: derive a lookup from a stored listing, persist the lead.

Thin orchestration around the pure ``EnrichmentService``: it loads the listing to
learn the company/person/known-e-mail, runs the network lookup outside any
transaction, then records a ``contact_leads`` row.
"""

from collections.abc import Mapping
from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.db import session_scope
from project_pilot.enrichment.extract import extract_emails
from project_pilot.enrichment.schemas import ContactEnrichment
from project_pilot.enrichment.service import EnrichmentService
from project_pilot.errors import EnrichmentError
from project_pilot.ingestion.normalize import extract_contact_person, looks_like_company
from project_pilot.models import ContactLead
from project_pilot.repository import Repository


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def derive_contact(
    raw: Mapping[str, object], description: str
) -> tuple[str | None, str | None, str | None]:
    """Pull (company, contact-person, known-e-mail) out of a listing's raw record + text.

    The structured first/last name is the real person for direct posts but the agency
    name for brokered ones, so a company-looking name is dropped and the person is
    taken from the description's "Ansprechpartner" label instead.
    """
    company = _str(raw.get("company"))
    name_parts = (_str(raw.get("firstName")), _str(raw.get("lastName")))
    structured = " ".join(part for part in name_parts if part)
    if structured and not looks_like_company(structured) and structured != company:
        person: str | None = structured
    else:
        person = extract_contact_person(description)
    emails = extract_emails(description)
    return company, person, emails[0] if emails else None


class ListingEnrichmentService:
    """Enriches a stored listing's company and records the result."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service: EnrichmentService,
    ) -> None:
        self._session_factory = session_factory
        self._service = service

    async def enrich_listing(self, listing_id: int) -> ContactEnrichment:
        """Look up the listing's company contacts and store a ``contact_leads`` row."""
        async with session_scope(self._session_factory) as session:
            listing = await Repository(session).get_listing(listing_id)
            if listing is None:
                raise EnrichmentError(f"Listing {listing_id} not found")
            company, person, known_email = derive_contact(
                listing.raw or {}, listing.description or ""
            )
            title = listing.title

        # The network lookup runs outside any unit of work (no transaction held open).
        result = await self._service.enrich(
            company=company, person=person, title=title, known_email=known_email
        )

        async with session_scope(self._session_factory) as session:
            await Repository(session).add_contact_lead(
                ContactLead(
                    listing_id=listing_id,
                    company=result.company,
                    person=result.person,
                    website=result.website,
                    emails=result.emails,
                    phones=result.phones,
                    persons=result.persons,
                    sources=result.sources,
                    links=asdict(result.links),
                    linkedin_message=result.linkedin_message,
                )
            )
        return result

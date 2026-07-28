"""Tests for listing-derived enrichment: subject derivation (pure) + persistence (DB)."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.enrichment.fetch import FetchedPage
from project_pilot.enrichment.listing import ListingEnrichmentService, derive_contact
from project_pilot.enrichment.schemas import SearchResult
from project_pilot.enrichment.service import EnrichmentService
from project_pilot.models import Listing, ListingStatus
from project_pilot.repository import Repository


def test_derive_contact_uses_structured_person() -> None:
    raw = {"company": "Muster GmbH", "firstName": "Max", "lastName": "Mustermann"}
    company, person, email = derive_contact(raw, "Bewerbung an bewerbung@muster.de")
    assert company == "Muster GmbH"
    assert person == "Max Mustermann"
    assert email == "bewerbung@muster.de"


def test_derive_contact_falls_back_to_description_label() -> None:
    raw = {"company": "Hays AG"}  # agency post: no direct person in the structured record
    company, person, email = derive_contact(raw, "Ansprechpartner: Julia Weber. Kontakt folgt.")
    assert company == "Hays AG"
    assert person == "Julia Weber"
    assert email is None


def test_derive_contact_without_company_or_person() -> None:
    assert derive_contact({}, "kein Kontakt hier") == (None, None, None)


class _EmptyFetcher:
    async def fetch(self, url: str) -> FetchedPage:
        return FetchedPage(url=url, text="<html></html>")

    async def aclose(self) -> None:
        return None


class _EmptySearch:
    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        return []


async def test_enrich_listing_persists_a_contact_lead(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing = Listing(
        source="freelancermap",
        external_url="https://www.freelancermap.de/projekt/x",
        url_hash="a" * 64,
        title="KI-Projekt",
        description="Ansprechpartner: Max Mustermann",
        raw={"company": "Muster GmbH", "firstName": "Max", "lastName": "Mustermann"},
        status=ListingStatus.EVALUATED,
    )
    session.add(listing)
    await session.commit()

    service = EnrichmentService(fetcher=_EmptyFetcher(), search=_EmptySearch())
    listing_service = ListingEnrichmentService(session_factory=session_factory, service=service)

    result = await listing_service.enrich_listing(listing.id)
    assert result.company == "Muster GmbH"
    assert result.person == "Max Mustermann"

    leads = await Repository(session).get_contact_leads(listing.id)
    assert len(leads) == 1
    assert leads[0].company == "Muster GmbH"
    assert leads[0].links["linkedin_company"]  # research links stored as JSON
    assert leads[0].linkedin_message.startswith("Hallo Max,")  # connection message stored

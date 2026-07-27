"""Tests for the Notion sales-pipeline integration (builders, client, service)."""

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.config import NotionConfig
from project_pilot.errors import NotionError
from project_pilot.models import (
    Application,
    ApplicationStatus,
    Evaluation,
    EvaluationStage,
    Listing,
    Verdict,
)
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.notion import (
    NOTION_PAGES_URL,
    NotionClient,
    NotionSalesPipeline,
    SalesLead,
    lead_from_application,
    lead_from_match,
    lead_properties,
)
from project_pilot.pipeline import to_match_message


def _match(
    *,
    company: str | None = "Talent Co GmbH",
    is_endcustomer: bool | None = False,
    contract_type: str | None = "Freelance",
) -> MatchMessage:
    return MatchMessage(
        title="AI Engineer (m/w/d)",
        url="https://www.freelancermap.de/projekt/ai-engineer",
        score=84,
        company=company,
        contact_name="Jane Doe",
        is_endcustomer=is_endcustomer,
        location="Berlin",
        start="ASAP",
        contract_type=contract_type,
        skills=["Python", "RAG"],
    )


def _application(
    *,
    status: ApplicationStatus = ApplicationStatus.READY,
    listing_id: int | None = None,
    url: str | None = "https://www.freelancermap.de/projekt/ai-engineer",
    sent_at: datetime | None = None,
) -> Application:
    return Application(
        listing_id=listing_id,
        listing_url=url,
        listing_title="AI Engineer (m/w/d)",
        recipient_email="pm@firma.de",
        subject="Bewerbung: AI Engineer",
        status=status,
        sent_at=sent_at,
    )


def test_lead_from_match_uses_company_name_and_summarizes() -> None:
    lead = lead_from_match(_match())
    assert lead.name == "Talent Co GmbH"
    assert lead.status == "Lead"
    assert lead.customer_type == "Recruiter"
    assert lead.contract_types == ["Freelance"]
    assert lead.lead_source == "Freelancermap"
    assert lead.url == "https://www.freelancermap.de/projekt/ai-engineer"
    assert "Project: AI Engineer (m/w/d)" in lead.notes
    assert "Match score: 84/100" in lead.notes
    assert "Contact: Jane Doe" in lead.notes
    assert "Skills: Python, RAG" in lead.notes


def test_lead_from_match_without_company_falls_back_to_title() -> None:
    lead = lead_from_match(_match(company=None, is_endcustomer=True))
    assert lead.name == "AI Engineer (m/w/d)"
    assert lead.customer_type == "Direct"
    assert "Project:" not in lead.notes  # the name already carries the title


def test_lead_from_match_maps_contract_labels_and_drops_unknown() -> None:
    assert lead_from_match(_match(contract_type="Employee leasing")).contract_types == ["ANÜ"]
    # unmapped labels must not create new multi-select options in Notion
    assert lead_from_match(_match(contract_type="Permanent position")).contract_types == []
    assert lead_from_match(_match(contract_type=None, is_endcustomer=None)).customer_type is None


def test_lead_from_application_open_draft_is_lead() -> None:
    lead = lead_from_application(_application())
    assert lead.name == "AI Engineer (m/w/d)"
    assert lead.status == "Lead"
    assert lead.lead_source == "Freelancermap"
    assert "Application drafted (not sent yet)" in lead.notes
    assert "Recipient: pm@firma.de" in lead.notes
    assert "Subject: Bewerbung: AI Engineer" in lead.notes


def test_lead_from_application_sent_is_contacted_with_date() -> None:
    sent_at = datetime(2026, 7, 27, 9, 30, tzinfo=UTC)
    lead = lead_from_application(_application(status=ApplicationStatus.SENT, sent_at=sent_at))
    assert lead.status == "Contacted"
    assert "Application sent: 27.07.2026" in lead.notes


def test_lead_from_application_off_platform_text_has_no_source() -> None:
    lead = lead_from_application(_application(url=None))
    assert lead.lead_source is None and lead.url is None


def test_lead_from_application_enriched_by_match_keeps_company_and_score() -> None:
    lead = lead_from_application(_application(), _match())
    assert lead.name == "Talent Co GmbH"
    assert lead.customer_type == "Recruiter"
    assert "Match score: 84/100" in lead.notes
    assert "Application drafted (not sent yet)" in lead.notes


def test_lead_properties_mirror_schema_and_omit_empty() -> None:
    full = lead_properties(lead_from_match(_match()))
    assert set(full) == {
        "Name",
        "Status",
        "Lead Source",
        "Customer Type",
        "Contract Type",
        "Link",
        "Notes",
    }
    assert full["Status"] == {"status": {"name": "Lead"}}
    assert full["Lead Source"] == {"select": {"name": "Freelancermap"}}
    assert full["Contract Type"] == {"multi_select": [{"name": "Freelance"}]}

    minimal = lead_properties(SalesLead(name="X", status="Lead", notes=""))
    assert set(minimal) == {"Name", "Status"}


def _client() -> NotionClient:
    return NotionClient(NotionConfig(token="secret-token", database_id="db-123"))


@respx.mock
async def test_create_lead_posts_payload_and_returns_page_url() -> None:
    route = respx.post(NOTION_PAGES_URL).mock(
        return_value=httpx.Response(200, json={"url": "https://www.notion.so/lead-1"})
    )
    url = await _client().create_lead(lead_from_match(_match()))
    assert url == "https://www.notion.so/lead-1"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret-token"
    assert request.headers["notion-version"]
    payload = json.loads(request.content)
    assert payload["parent"] == {"database_id": "db-123"}
    assert payload["properties"]["Name"]["title"][0]["text"]["content"] == "Talent Co GmbH"


@respx.mock
async def test_create_lead_raises_notion_error_on_rejection() -> None:
    respx.post(NOTION_PAGES_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    with pytest.raises(NotionError, match="HTTP 401"):
        await _client().create_lead(lead_from_match(_match()))


@respx.mock
async def test_create_lead_raises_notion_error_on_transport_failure() -> None:
    respx.post(NOTION_PAGES_URL).mock(side_effect=httpx.ConnectError("network down"))
    with pytest.raises(NotionError, match="request failed"):
        await _client().create_lead(lead_from_match(_match()))


class _FakeCreator:
    def __init__(self) -> None:
        self.leads: list[SalesLead] = []

    async def create_lead(self, lead: SalesLead) -> str:
        self.leads.append(lead)
        return "https://www.notion.so/lead-1"


def _stored_listing() -> Listing:
    return Listing(
        source="freelancermap",
        external_url="https://www.freelancermap.de/projekt/ai-engineer",
        url_hash="a" * 64,
        title="AI Engineer (m/w/d)",
        description="LLM Projekt",
        skills=["Python"],
        location="Berlin",
        raw={"company": "Talent Co GmbH", "isEndcustomerProject": True},
        evaluations=[
            Evaluation(
                stage=EvaluationStage.LLM,
                verdict=Verdict.MATCH,
                score=84,
                reason={"reasons": ["fits"]},
            )
        ],
    )


async def test_service_add_listing_files_match_lead(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        listing = _stored_listing()
        session.add(listing)
        await session.commit()
        listing_id = listing.id
    creator = _FakeCreator()
    service = NotionSalesPipeline(
        session_factory=session_factory, client=creator, build_message=to_match_message
    )
    url = await service.add_listing(listing_id)
    assert url == "https://www.notion.so/lead-1"
    (lead,) = creator.leads
    assert lead.name == "Talent Co GmbH"
    assert lead.customer_type == "Direct"
    assert "Match score: 84/100" in lead.notes


async def test_service_add_application_merges_listing_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        listing = _stored_listing()
        session.add(listing)
        await session.flush()
        application = _application(listing_id=listing.id, status=ApplicationStatus.SENT)
        session.add(application)
        await session.commit()
        application_id = application.id
    creator = _FakeCreator()
    service = NotionSalesPipeline(
        session_factory=session_factory, client=creator, build_message=to_match_message
    )
    await service.add_application(application_id)
    (lead,) = creator.leads
    assert lead.name == "Talent Co GmbH"
    assert lead.status == "Contacted"
    assert "Recipient: pm@firma.de" in lead.notes


async def test_service_missing_entities_raise_notion_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = NotionSalesPipeline(
        session_factory=session_factory, client=_FakeCreator(), build_message=to_match_message
    )
    with pytest.raises(NotionError, match="not found"):
        await service.add_listing(999)
    with pytest.raises(NotionError, match="not found"):
        await service.add_application(999)

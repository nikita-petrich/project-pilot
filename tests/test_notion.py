"""Tests for the Notion sales-pipeline integration (builders, client, service)."""

import json
from datetime import UTC, date, datetime

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
    NOTION_API,
    LeadPage,
    NotionClient,
    NotionSalesPipeline,
    SalesLead,
    advance_properties,
    lead_blocks,
    lead_from_application,
    lead_from_match,
    lead_properties,
)
from project_pilot.pipeline import stored_match_message

SENT_AT = datetime(2026, 7, 27, 9, 30, tzinfo=UTC)
LISTING_URL = "https://www.freelancermap.de/projekt/ai-engineer"


def _match(
    *,
    company: str | None = "Talent Co GmbH",
    is_endcustomer: bool | None = False,
    contract_type: str | None = "Freelance",
    score: int = 84,
    start_date: date | None = date(2026, 9, 1),
) -> MatchMessage:
    return MatchMessage(
        title="AI Engineer (m/w/d)",
        url=LISTING_URL,
        score=score,
        company=company,
        contact_name="Jane Doe",
        is_endcustomer=is_endcustomer,
        location="Berlin",
        remote_label="80% (20% on-site)",
        contract_type=contract_type,
        workload_label="100%",
        duration_label="6 mo",
        start="01.09.2026",
        start_date=start_date,
        posted_ago="12 min ago",
        expires_label="15.08.2026",
        industry="IT",
        language="German",
        skills=["Python", "RAG"],
        reasons=["LLM focus"],
        matching_skills=["Python"],
        missing_requirements=["Kubernetes"],
        risk_flags=["tight start"],
        description="LLM Projekt mit RAG-Anteil.",
    )


def _application(
    *,
    status: ApplicationStatus = ApplicationStatus.READY,
    listing_id: int | None = None,
    url: str | None = LISTING_URL,
    sent_at: datetime | None = None,
) -> Application:
    return Application(
        listing_id=listing_id,
        listing_url=url,
        listing_title="AI Engineer (m/w/d)",
        contact_name="Jane Doe",
        recipient_email="pm@firma.de",
        subject="Bewerbung: AI Engineer",
        body="Sehr geehrte Frau Doe,\nich bewerbe mich.",
        linkedin_message="Hallo!",
        status=status,
        sent_at=sent_at,
    )


def test_lead_from_match_carries_every_known_field() -> None:
    lead = lead_from_match(_match())
    assert lead.name == "Talent Co GmbH"
    assert lead.status == "Lead"
    assert lead.customer_type == "Recruiter"
    assert lead.contract_types == ["Freelance"]
    assert lead.lead_source == "Freelancermap"
    assert lead.priority == "Medium"  # 84 is below the High cut-off
    assert lead.url == LISTING_URL
    assert lead.expected_close == date(2026, 9, 1)
    for line in (
        "Project: AI Engineer (m/w/d)",
        "Match score: 84/100",
        "Client type: recruiter/agency",
        "Contact: Jane Doe",
        "Location: Berlin",
        "Remote: 80% (20% on-site)",
        "Contract: Freelance",
        "Workload: 100%",
        "Duration: 6 mo",
        "Start: 01.09.2026",
        "Posted: 12 min ago",
        "Apply by: 15.08.2026",
        "Industry: IT",
        "Language: German",
        "Skills: Python, RAG",
        "Fits: LLM focus",
        "Your skills: Python",
        "Gaps: Kubernetes",
        "Risks: tight start",
    ):
        assert line in lead.notes
    assert lead.sections == [("Project description", "LLM Projekt mit RAG-Anteil.")]


def test_lead_from_match_without_company_falls_back_to_title() -> None:
    lead = lead_from_match(_match(company=None, is_endcustomer=True))
    assert lead.name == "AI Engineer (m/w/d)"
    assert lead.customer_type == "Direct"
    assert "Project:" not in lead.notes  # the name already carries the title


def test_lead_priority_follows_the_match_score() -> None:
    assert lead_from_match(_match(score=90)).priority == "High"
    assert lead_from_match(_match(score=70)).priority == "Medium"
    assert lead_from_match(_match(score=61)).priority == "Low"


def test_lead_from_match_maps_contract_labels_and_drops_unknown() -> None:
    assert lead_from_match(_match(contract_type="Employee leasing")).contract_types == ["ANÜ"]
    # unmapped labels must not create new multi-select options in Notion
    assert lead_from_match(_match(contract_type="Permanent position")).contract_types == []
    assert lead_from_match(_match(is_endcustomer=None)).customer_type is None


def test_lead_from_application_open_draft_is_lead_without_dates() -> None:
    lead = lead_from_application(_application())
    assert lead.name == "AI Engineer (m/w/d)"
    assert lead.status == "Lead"
    assert lead.lead_source == "Freelancermap"
    assert lead.last_contact is None and lead.follow_up is None
    assert "Application: drafted (not sent yet)" in lead.notes
    assert "Recipient: pm@firma.de" in lead.notes
    assert "Subject: Bewerbung: AI Engineer" in lead.notes
    assert ("Application draft", "Sehr geehrte Frau Doe,\nich bewerbe mich.") in lead.sections
    assert ("LinkedIn message", "Hallo!") in lead.sections


def test_lead_from_sent_application_is_contacted_with_dates_and_email() -> None:
    lead = lead_from_application(_application(status=ApplicationStatus.SENT, sent_at=SENT_AT))
    assert lead.status == "Contacted"
    assert lead.last_contact == date(2026, 7, 27)
    assert lead.follow_up == date(2026, 8, 3)  # one week later
    assert "Application: sent 27.07.2026" in lead.notes
    assert ("Application e-mail", "Sehr geehrte Frau Doe,\nich bewerbe mich.") in lead.sections
    assert "pm@firma.de" in lead.event and "27.07.2026" in lead.event


def test_lead_from_application_off_platform_text_has_no_source() -> None:
    lead = lead_from_application(_application(url=None))
    assert lead.lead_source is None and lead.url is None


def test_lead_from_application_enriched_by_match_keeps_listing_facts() -> None:
    lead = lead_from_application(_application(), _match())
    assert lead.name == "Talent Co GmbH"
    assert lead.customer_type == "Recruiter"
    assert lead.priority == "Medium"
    assert lead.expected_close == date(2026, 9, 1)
    assert "Match score: 84/100" in lead.notes
    assert "Application: drafted (not sent yet)" in lead.notes
    # both the listing description and the draft ride along in the page body
    assert [title for title, _ in lead.sections] == [
        "Project description",
        "Application draft",
        "LinkedIn message",
    ]


def test_lead_properties_mirror_schema_and_omit_empty() -> None:
    full = lead_properties(
        lead_from_application(
            _application(status=ApplicationStatus.SENT, sent_at=SENT_AT), _match()
        )
    )
    assert set(full) == {
        "Name",
        "Status",
        "Lead Source",
        "Customer Type",
        "Contract Type",
        "Priority",
        "Link",
        "Notes",
        "Last Contact",
        "Follow Up Date",
        "Expected Close",
    }
    assert full["Status"] == {"status": {"name": "Contacted"}}
    assert full["Lead Source"] == {"select": {"name": "Freelancermap"}}
    assert full["Contract Type"] == {"multi_select": [{"name": "Freelance"}]}
    assert full["Last Contact"] == {"date": {"start": "2026-07-27"}}

    minimal = lead_properties(SalesLead(name="X", status="Lead", notes=""))
    assert set(minimal) == {"Name", "Status"}


def test_lead_notes_stay_within_the_notion_text_limit() -> None:
    lead = lead_from_match(_match())
    notes = SalesLead(name="X", status="Lead", notes="wort " * 1000)
    rendered = lead_properties(notes)["Notes"]
    assert isinstance(rendered, dict)
    rich_text = rendered["rich_text"]
    assert isinstance(rich_text, list)
    assert len(str(rich_text[0]["text"]["content"])) <= 2000
    assert lead.notes  # the real summary is far below the limit


def test_lead_blocks_split_long_sections_without_dropping_text() -> None:
    body = "\n".join(f"Zeile {i}: " + "wort " * 40 for i in range(200))
    lead = SalesLead(name="X", status="Lead", notes="", sections=[("E-mail", body)])
    blocks = lead_blocks(lead)
    assert blocks[0]["type"] == "heading_3"
    paragraphs = [b for b in blocks if b["type"] == "paragraph"]
    assert len(paragraphs) >= 2
    for block in paragraphs:
        paragraph = block["paragraph"]
        assert isinstance(paragraph, dict)
        rich_text = paragraph["rich_text"]
        assert isinstance(rich_text, list)
        assert len(str(rich_text[0]["text"]["content"])) <= 2000


def test_advance_properties_only_move_the_status_forward() -> None:
    sent = lead_from_application(_application(status=ApplicationStatus.SENT, sent_at=SENT_AT))
    fresh = advance_properties(sent, None)
    assert fresh["Status"] == {"status": {"name": "Contacted"}}
    assert fresh["Last Contact"] == {"date": {"start": "2026-07-27"}}
    assert fresh["Follow Up Date"] == {"date": {"start": "2026-08-03"}}
    # Notes, priority and every other manual edit stay untouched
    assert set(fresh) == {"Status", "Last Contact", "Follow Up Date"}

    from_lead = advance_properties(sent, "Lead")
    assert from_lead["Status"] == {"status": {"name": "Contacted"}}
    # a deal already further along is never reset
    assert "Status" not in advance_properties(sent, "Negotiation")
    assert "Status" not in advance_properties(sent, "Won")
    assert "Status" not in advance_properties(sent, "Contacted")


def _client() -> NotionClient:
    return NotionClient(NotionConfig(token="secret-token", database_id="db-123"))


def _page_response(page_id: str = "page-1") -> dict[str, object]:
    return {
        "id": page_id,
        "url": "https://www.notion.so/lead-1",
        "properties": {"Status": {"status": {"name": "Lead"}}},
    }


@respx.mock
async def test_create_posts_properties_and_body() -> None:
    route = respx.post(f"{NOTION_API}/pages").mock(
        return_value=httpx.Response(200, json=_page_response())
    )
    page = await _client().create(lead_from_match(_match()))
    assert page == LeadPage(page_id="page-1", url="https://www.notion.so/lead-1", status="Lead")
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret-token"
    assert request.headers["notion-version"]
    payload = json.loads(request.content)
    assert payload["parent"] == {"database_id": "db-123"}
    assert payload["properties"]["Name"]["title"][0]["text"]["content"] == "Talent Co GmbH"
    assert payload["children"][0]["type"] == "heading_3"


@respx.mock
async def test_find_by_link_returns_the_existing_entry() -> None:
    route = respx.post(f"{NOTION_API}/databases/db-123/query").mock(
        return_value=httpx.Response(200, json={"results": [_page_response("known")]})
    )
    page = await _client().find_by_link(LISTING_URL)
    assert page is not None and page.page_id == "known" and page.status == "Lead"
    payload = json.loads(route.calls.last.request.content)
    assert payload["filter"] == {"property": "Link", "url": {"equals": LISTING_URL}}


@respx.mock
async def test_find_by_link_returns_none_when_unknown() -> None:
    respx.post(f"{NOTION_API}/databases/db-123/query").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    assert await _client().find_by_link(LISTING_URL) is None


@respx.mock
async def test_advance_patches_properties_and_appends_the_event() -> None:
    patch = respx.patch(f"{NOTION_API}/pages/known").mock(
        return_value=httpx.Response(200, json=_page_response("known"))
    )
    append = respx.patch(f"{NOTION_API}/blocks/known/children").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    lead = lead_from_application(_application(status=ApplicationStatus.SENT, sent_at=SENT_AT))
    page = LeadPage(page_id="known", url="https://www.notion.so/lead-1", status="Lead")
    await _client().advance(page, lead, now=SENT_AT)
    assert json.loads(patch.calls.last.request.content)["properties"]["Status"] == {
        "status": {"name": "Contacted"}
    }
    appended = json.loads(append.calls.last.request.content)["children"][0]
    assert "pm@firma.de" in appended["paragraph"]["rich_text"][0]["text"]["content"]


@respx.mock
async def test_request_raises_notion_error_on_rejection() -> None:
    respx.post(f"{NOTION_API}/pages").mock(return_value=httpx.Response(401, text="unauthorized"))
    with pytest.raises(NotionError, match="HTTP 401"):
        await _client().create(lead_from_match(_match()))


@respx.mock
async def test_request_raises_notion_error_on_transport_failure() -> None:
    respx.post(f"{NOTION_API}/pages").mock(side_effect=httpx.ConnectError("network down"))
    with pytest.raises(NotionError, match="request failed"):
        await _client().create(lead_from_match(_match()))


class _FakeStore:
    """In-memory ``LeadStore``: remembers created leads and advance calls."""

    def __init__(self, *, existing: LeadPage | None = None) -> None:
        self.existing = existing
        self.created: list[SalesLead] = []
        self.advanced: list[tuple[LeadPage, SalesLead]] = []
        self.looked_up: list[str] = []

    async def find_by_link(self, url: str) -> LeadPage | None:
        self.looked_up.append(url)
        return self.existing

    async def create(self, lead: SalesLead) -> LeadPage:
        self.created.append(lead)
        return LeadPage(page_id="new", url="https://www.notion.so/lead-new", status=lead.status)

    async def advance(self, page: LeadPage, lead: SalesLead, *, now: datetime) -> LeadPage:
        self.advanced.append((page, lead))
        return page


def _stored_listing() -> Listing:
    return Listing(
        source="freelancermap",
        external_url=LISTING_URL,
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


def _service(
    session_factory: async_sessionmaker[AsyncSession], store: _FakeStore
) -> NotionSalesPipeline:
    return NotionSalesPipeline(
        session_factory=session_factory, client=store, build_message=stored_match_message
    )


async def test_service_add_listing_files_a_new_lead(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        listing = _stored_listing()
        session.add(listing)
        await session.commit()
        listing_id = listing.id
    store = _FakeStore()
    filed = await _service(session_factory, store).add_listing(listing_id)
    assert filed.url == "https://www.notion.so/lead-new" and filed.created
    (lead,) = store.created
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
        application = _application(
            listing_id=listing.id, status=ApplicationStatus.SENT, sent_at=SENT_AT
        )
        session.add(application)
        await session.commit()
        application_id = application.id
    store = _FakeStore()
    await _service(session_factory, store).add_application(application_id)
    (lead,) = store.created
    assert lead.name == "Talent Co GmbH"
    assert lead.status == "Contacted"
    assert lead.last_contact == date(2026, 7, 27)
    assert "Recipient: pm@firma.de" in lead.notes


async def test_service_advances_a_project_already_in_the_pipeline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        listing = _stored_listing()
        session.add(listing)
        await session.flush()
        application = _application(
            listing_id=listing.id, status=ApplicationStatus.SENT, sent_at=SENT_AT
        )
        session.add(application)
        await session.commit()
        application_id = application.id
    known = LeadPage(page_id="known", url="https://www.notion.so/lead-1", status="Lead")
    store = _FakeStore(existing=known)
    filed = await _service(session_factory, store).add_application(application_id)
    assert filed.url == "https://www.notion.so/lead-1" and not filed.created
    assert store.created == []  # no duplicate row in the pipeline
    (page, lead) = store.advanced[0]
    assert page is known and lead.status == "Contacted"
    assert store.looked_up == [LISTING_URL]


async def test_service_missing_entities_raise_notion_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(session_factory, _FakeStore())
    with pytest.raises(NotionError, match="not found"):
        await service.add_listing(999)
    with pytest.raises(NotionError, match="not found"):
        await service.add_application(999)

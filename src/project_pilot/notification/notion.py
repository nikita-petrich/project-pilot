"""Notion sales-pipeline integration: lead building and a lean API client.

One button click on a match or draft message files the project as a lead in the
Notion "Sales Pipeline" database. Pure builders turn display/application data
into a ``SalesLead``; ``NotionClient`` writes it via the REST API (no SDK); the
``NotionSalesPipeline`` service loads the entities and connects the two.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.config import NotionConfig
from project_pilot.db import session_scope
from project_pilot.errors import NotionError
from project_pilot.models import Application, ApplicationStatus, Listing
from project_pilot.notification.messages import MatchMessage
from project_pilot.repository import Repository

logger = logging.getLogger(__name__)

NOTION_PAGES_URL = "https://api.notion.com/v1/pages"
NOTION_VERSION = "2022-06-28"

# Property names and option labels of the Sales Pipeline database — centralized so a
# schema change in Notion is adapted in one place.
PROP_NAME = "Name"
PROP_STATUS = "Status"
PROP_LEAD_SOURCE = "Lead Source"
PROP_CUSTOMER_TYPE = "Customer Type"
PROP_CONTRACT_TYPE = "Contract Type"
PROP_LINK = "Link"
PROP_NOTES = "Notes"

STATUS_LEAD = "Lead"
STATUS_CONTACTED = "Contacted"
LEAD_SOURCE_FREELANCERMAP = "Freelancermap"

# Match-message contract labels → the database's multi-select options. Unmapped labels
# are dropped so the API never creates new select options on the fly.
_CONTRACT_OPTIONS = {
    "Freelance": "Freelance",
    "Employee leasing": "ANÜ",
    "Temporary employment": "ANÜ",
}

_NAME_LIMIT = 200
_NOTES_LIMIT = 1900  # Notion caps one rich_text fragment at 2000 chars
_SKILLS_IN_NOTES = 10


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True, slots=True)
class SalesLead:
    """The data written to one Sales Pipeline entry."""

    name: str
    status: str
    notes: str
    url: str | None = None
    customer_type: str | None = None
    contract_types: list[str] = field(default_factory=list)
    lead_source: str | None = None


def lead_from_match(message: MatchMessage) -> SalesLead:
    """Build a pipeline lead from a match message (company-named when possible)."""
    lines: list[str] = []
    if message.company:
        lines.append(f"Project: {message.title}")
    lines.append(f"Match score: {message.score}/100")
    for label, value in (
        ("Contact", message.contact_name),
        ("Location", message.location),
        ("Remote", message.remote_label),
        ("Start", message.start),
        ("Duration", message.duration_label),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if message.skills:
        lines.append("Skills: " + ", ".join(message.skills[:_SKILLS_IN_NOTES]))

    customer_type = None
    if message.is_endcustomer is not None:
        customer_type = "Direct" if message.is_endcustomer else "Recruiter"
    contract = _CONTRACT_OPTIONS.get(message.contract_type) if message.contract_type else None

    return SalesLead(
        name=_clip(message.company or message.title, _NAME_LIMIT),
        status=STATUS_LEAD,
        notes="\n".join(lines),
        url=message.url or None,
        customer_type=customer_type,
        contract_types=[contract] if contract else [],
        lead_source=LEAD_SOURCE_FREELANCERMAP,
    )


def lead_from_application(application: Application, match: MatchMessage | None = None) -> SalesLead:
    """Build a pipeline lead from an application; ``match`` data enriches it when known.

    A sent application files as "Contacted", an open draft as "Lead".
    """
    base = lead_from_match(match) if match is not None else None
    sent = application.status is ApplicationStatus.SENT

    lines = [base.notes] if base is not None and base.notes else []
    if sent and application.sent_at is not None:
        lines.append(f"Application sent: {application.sent_at.strftime('%d.%m.%Y')}")
    elif sent:
        lines.append("Application sent")
    else:
        lines.append("Application drafted (not sent yet)")
    if application.recipient_email:
        lines.append(f"Recipient: {application.recipient_email}")
    if application.subject:
        lines.append(f"Subject: {application.subject}")

    url = application.listing_url or (base.url if base is not None else None)
    if base is not None:
        lead_source = base.lead_source
    else:
        lead_source = LEAD_SOURCE_FREELANCERMAP if url and "freelancermap." in url else None

    return SalesLead(
        name=base.name if base is not None else _clip(application.listing_title, _NAME_LIMIT),
        status=STATUS_CONTACTED if sent else STATUS_LEAD,
        notes="\n".join(lines),
        url=url,
        customer_type=base.customer_type if base is not None else None,
        contract_types=list(base.contract_types) if base is not None else [],
        lead_source=lead_source,
    )


def _rich_text(content: str) -> list[dict[str, object]]:
    return [{"text": {"content": content}}]


def lead_properties(lead: SalesLead) -> dict[str, object]:
    """The Notion page properties for one lead (keys mirror the database schema)."""
    properties: dict[str, object] = {
        PROP_NAME: {"title": _rich_text(lead.name)},
        PROP_STATUS: {"status": {"name": lead.status}},
    }
    if lead.lead_source:
        properties[PROP_LEAD_SOURCE] = {"select": {"name": lead.lead_source}}
    if lead.customer_type:
        properties[PROP_CUSTOMER_TYPE] = {"select": {"name": lead.customer_type}}
    if lead.contract_types:
        properties[PROP_CONTRACT_TYPE] = {
            "multi_select": [{"name": option} for option in lead.contract_types]
        }
    if lead.url:
        properties[PROP_LINK] = {"url": lead.url}
    if lead.notes:
        properties[PROP_NOTES] = {"rich_text": _rich_text(_clip(lead.notes, _NOTES_LIMIT))}
    return properties


class NotionClient:
    """Creates Sales Pipeline pages via the Notion REST API (one endpoint, no SDK)."""

    def __init__(self, config: NotionConfig, *, timeout: float = 15.0) -> None:
        self._config = config
        self._timeout = timeout

    async def create_lead(self, lead: SalesLead) -> str:
        """File ``lead`` as a new page and return its Notion URL (empty when absent)."""
        payload = {
            "parent": {"database_id": self._config.database_id},
            "properties": lead_properties(lead),
        }
        headers = {
            "Authorization": f"Bearer {self._config.token}",
            "Notion-Version": NOTION_VERSION,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                response = await http.post(NOTION_PAGES_URL, json=payload, headers=headers)
        except httpx.HTTPError as err:
            raise NotionError(f"Notion request failed: {err}") from err
        if response.status_code != 200:
            raise NotionError(
                f"Notion rejected the lead (HTTP {response.status_code}): {response.text[:300]}"
            )
        data: object = response.json()
        url = data.get("url") if isinstance(data, dict) else None
        return url if isinstance(url, str) else ""


class LeadCreator(Protocol):
    """The Notion writing surface the service needs (``NotionClient`` satisfies it)."""

    async def create_lead(self, lead: SalesLead) -> str: ...


type MatchMessageBuilder = Callable[[Listing, datetime], MatchMessage]


class NotionSalesPipeline:
    """Files listings and applications as Sales Pipeline leads (button-click handler)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: LeadCreator,
        build_message: MatchMessageBuilder,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._build_message = build_message

    async def add_listing(self, listing_id: int) -> str:
        """Create a lead from a stored listing (the match message's Notion button)."""
        async with session_scope(self._session_factory) as session:
            listing = await Repository(session).get_listing_with_evaluations(listing_id)
            if listing is None:
                raise NotionError(f"Project {listing_id} not found")
            lead = lead_from_match(self._build_message(listing, _utcnow()))
        # The API call happens outside the unit of work: a slow Notion response must
        # not hold a database transaction open.
        return await self._client.create_lead(lead)

    async def add_application(self, application_id: int) -> str:
        """Create a lead from an application draft (the draft message's Notion button)."""
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            application = await repo.get_application(application_id)
            if application is None:
                raise NotionError(f"Draft {application_id} not found")
            listing = (
                await repo.get_listing_with_evaluations(application.listing_id)
                if application.listing_id is not None
                else None
            )
            match = self._build_message(listing, _utcnow()) if listing is not None else None
            lead = lead_from_application(application, match)
        return await self._client.create_lead(lead)

"""Notion sales-pipeline integration: lead building and a lean API client.

A match or draft message files the project as a lead in the Notion "Sales
Pipeline" database, and sending an application files (or advances) it
automatically. Pure builders turn display/application data into a ``SalesLead``;
``NotionClient`` reads and writes it via the REST API (no SDK); the
``NotionSalesPipeline`` service loads the entities and ties the two together.

Filing is an upsert keyed on the listing link: a project already in the pipeline
is advanced in place instead of duplicated, and manual edits survive — an update
only moves the status forward, refreshes the contact dates, and appends what
happened to the page body.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Property names and option labels of the Sales Pipeline database — centralized so a
# schema change in Notion is adapted in one place.
PROP_NAME = "Name"
PROP_STATUS = "Status"
PROP_LEAD_SOURCE = "Lead Source"
PROP_CUSTOMER_TYPE = "Customer Type"
PROP_CONTRACT_TYPE = "Contract Type"
PROP_PRIORITY = "Priority"
PROP_LINK = "Link"
PROP_NOTES = "Notes"
PROP_LAST_CONTACT = "Last Contact"
PROP_FOLLOW_UP = "Follow Up Date"
PROP_EXPECTED_CLOSE = "Expected Close"

STATUS_LEAD = "Lead"
STATUS_CONTACTED = "Contacted"
LEAD_SOURCE_FREELANCERMAP = "Freelancermap"

# The pipeline's forward order. An upsert never moves a record backwards, so a deal
# already in negotiation is not reset to "Contacted" by a second application.
_STATUS_ORDER = (
    STATUS_LEAD,
    STATUS_CONTACTED,
    "Qualified",
    "Proposal",
    "Negotiation",
    "Won",
    "Lost",
    "Closed",
)

# Match-message contract labels → the database's multi-select options. Unmapped labels
# are dropped so the API never creates new select options on the fly.
_CONTRACT_OPTIONS = {
    "Freelance": "Freelance",
    "Employee leasing": "ANÜ",
    "Temporary employment": "ANÜ",
}

# A strong match deserves a closer follow-up than a borderline one.
_PRIORITY_HIGH_SCORE = 85
_PRIORITY_MEDIUM_SCORE = 70

FOLLOW_UP_DAYS = 7  # a sent application without a reply is worth a nudge after a week

_NAME_LIMIT = 200
_TEXT_LIMIT = 1900  # Notion caps one rich_text fragment at 2000 chars
_PARAGRAPH_LIMIT = 1900
_MAX_BODY_BLOCKS = 40
_SKILLS_IN_NOTES = 15


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _chunks(text: str, limit: int) -> list[str]:
    """Split ``text`` into ``<= limit`` pieces on line boundaries; nothing is dropped."""
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


@dataclass(frozen=True, slots=True)
class SalesLead:
    """Everything one Sales Pipeline entry carries (properties plus page body)."""

    name: str
    status: str
    notes: str
    url: str | None = None
    customer_type: str | None = None
    contract_types: list[str] = field(default_factory=list)
    lead_source: str | None = None
    priority: str | None = None
    last_contact: date | None = None
    follow_up: date | None = None
    expected_close: date | None = None
    # Page body: (heading, text) sections holding the long-form record.
    sections: list[tuple[str, str]] = field(default_factory=list)
    # One-line summary of what this filing did, appended when advancing a known page.
    event: str = ""


def _priority(score: int) -> str:
    if score >= _PRIORITY_HIGH_SCORE:
        return "High"
    return "Medium" if score >= _PRIORITY_MEDIUM_SCORE else "Low"


def _labeled_lines(pairs: list[tuple[str, str | None]]) -> list[str]:
    return [f"{label}: {value}" for label, value in pairs if value]


def lead_from_match(message: MatchMessage) -> SalesLead:
    """Build a pipeline lead from a match message (company-named when possible)."""
    client_type = None
    if message.is_endcustomer is not None:
        client_type = "direct client" if message.is_endcustomer else "recruiter/agency"

    lines = _labeled_lines(
        [
            ("Project", message.title if message.company else None),
            ("Match score", f"{message.score}/100"),
            ("Client type", client_type),
            ("Contact", message.contact_name),
            ("Location", message.location),
            ("Remote", message.remote_label),
            ("Contract", message.contract_type),
            ("Workload", message.workload_label),
            ("Duration", message.duration_label),
            ("Start", message.start),
            ("Posted", message.posted_ago),
            ("Apply by", message.expires_label),
            ("Industry", message.industry),
            ("Language", message.language),
        ]
    )
    for label, values, limit in (
        ("Skills", message.skills, _SKILLS_IN_NOTES),
        ("Fits", message.reasons, 5),
        ("Your skills", message.matching_skills, 10),
        ("Gaps", message.missing_requirements, 5),
        ("Risks", message.risk_flags, 5),
    ):
        picked = [value for value in values if value][:limit]
        if picked:
            lines.append(f"{label}: {', '.join(picked)}")

    contract = _CONTRACT_OPTIONS.get(message.contract_type) if message.contract_type else None
    sections = [("Project description", message.description)] if message.description else []

    return SalesLead(
        name=_clip(message.company or message.title, _NAME_LIMIT),
        status=STATUS_LEAD,
        notes="\n".join(lines),
        url=message.url or None,
        customer_type=_customer_type(message.is_endcustomer),
        contract_types=[contract] if contract else [],
        lead_source=LEAD_SOURCE_FREELANCERMAP,
        priority=_priority(message.score),
        expected_close=message.start_date,
        sections=sections,
        event=f"Filed from the match alert ({message.score}/100).",
    )


def _customer_type(is_endcustomer: bool | None) -> str | None:
    if is_endcustomer is None:
        return None
    return "Direct" if is_endcustomer else "Recruiter"


def lead_from_application(application: Application, match: MatchMessage | None = None) -> SalesLead:
    """Build a pipeline lead from an application; ``match`` data enriches it when known.

    A sent application files as "Contacted" and carries the send date, the follow-up
    reminder, and the full e-mail in the page body; an open draft stays a "Lead".
    """
    base = lead_from_match(match) if match is not None else None
    sent = application.status is ApplicationStatus.SENT
    sent_on = application.sent_at.date() if application.sent_at is not None else None

    lines = [base.notes] if base is not None and base.notes else []
    lines.extend(
        _labeled_lines(
            [
                (
                    "Application",
                    (f"sent {sent_on.strftime('%d.%m.%Y')}" if sent_on else "sent")
                    if sent
                    else "drafted (not sent yet)",
                ),
                ("Recipient", application.recipient_email),
                ("Contact", application.contact_name if base is None else None),
                ("Subject", application.subject),
                (
                    "Revisions",
                    str(application.revision_count) if application.revision_count else None,
                ),
            ]
        )
    )

    sections = list(base.sections) if base is not None else []
    if application.body:
        sections.append(("Application e-mail" if sent else "Application draft", application.body))
    if application.linkedin_message:
        sections.append(("LinkedIn message", application.linkedin_message))

    url = application.listing_url or (base.url if base is not None else None)
    if base is not None:
        lead_source = base.lead_source
    else:
        lead_source = LEAD_SOURCE_FREELANCERMAP if url and "freelancermap." in url else None

    last_contact = sent_on if sent else None
    follow_up = last_contact + timedelta(days=FOLLOW_UP_DAYS) if last_contact else None
    event = (
        f"Application sent to {application.recipient_email or 'the client'}"
        f"{f' on {sent_on.strftime("%d.%m.%Y")}' if sent_on else ''}."
        if sent
        else "Application drafted (not sent yet)."
    )

    return SalesLead(
        name=base.name if base is not None else _clip(application.listing_title, _NAME_LIMIT),
        status=STATUS_CONTACTED if sent else STATUS_LEAD,
        notes="\n".join(lines),
        url=url,
        customer_type=base.customer_type if base is not None else None,
        contract_types=list(base.contract_types) if base is not None else [],
        lead_source=lead_source,
        priority=base.priority if base is not None else None,
        last_contact=last_contact,
        follow_up=follow_up,
        expected_close=base.expected_close if base is not None else None,
        sections=sections,
        event=event,
    )


def _rich_text(content: str) -> list[dict[str, object]]:
    return [{"text": {"content": content}}]


def _date_property(value: date | None) -> dict[str, object] | None:
    return {"date": {"start": value.isoformat()}} if value is not None else None


def lead_properties(lead: SalesLead) -> dict[str, object]:
    """The Notion page properties for a newly filed lead (keys mirror the schema)."""
    properties: dict[str, object] = {
        PROP_NAME: {"title": _rich_text(lead.name)},
        PROP_STATUS: {"status": {"name": lead.status}},
    }
    for prop, option in (
        (PROP_LEAD_SOURCE, lead.lead_source),
        (PROP_CUSTOMER_TYPE, lead.customer_type),
        (PROP_PRIORITY, lead.priority),
    ):
        if option:
            properties[prop] = {"select": {"name": option}}
    if lead.contract_types:
        properties[PROP_CONTRACT_TYPE] = {
            "multi_select": [{"name": option} for option in lead.contract_types]
        }
    if lead.url:
        properties[PROP_LINK] = {"url": lead.url}
    if lead.notes:
        properties[PROP_NOTES] = {"rich_text": _rich_text(_clip(lead.notes, _TEXT_LIMIT))}
    for prop, value in (
        (PROP_LAST_CONTACT, lead.last_contact),
        (PROP_FOLLOW_UP, lead.follow_up),
        (PROP_EXPECTED_CLOSE, lead.expected_close),
    ):
        stamped = _date_property(value)
        if stamped is not None:
            properties[prop] = stamped
    return properties


def advance_properties(lead: SalesLead, current_status: str | None) -> dict[str, object]:
    """The properties an upsert writes onto an existing page.

    Deliberately narrow: status (forward only), the contact dates, and a link when the
    page has none. Notes, priority, and every manual edit stay untouched — what
    happened is appended to the page body instead.
    """
    properties: dict[str, object] = {}
    if _advances(current_status, lead.status):
        properties[PROP_STATUS] = {"status": {"name": lead.status}}
    for prop, value in (
        (PROP_LAST_CONTACT, lead.last_contact),
        (PROP_FOLLOW_UP, lead.follow_up),
    ):
        stamped = _date_property(value)
        if stamped is not None:
            properties[prop] = stamped
    return properties


def _advances(current: str | None, proposed: str) -> bool:
    """True when ``proposed`` sits later in the pipeline than ``current``."""
    if current is None:
        return True
    if current not in _STATUS_ORDER or proposed not in _STATUS_ORDER:
        return False
    return _STATUS_ORDER.index(proposed) > _STATUS_ORDER.index(current)


def _paragraph(text: str) -> dict[str, object]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text)},
    }


def _heading(text: str) -> dict[str, object]:
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": _rich_text(text)},
    }


def lead_blocks(lead: SalesLead) -> list[dict[str, object]]:
    """The page body: the long-form record (description, e-mail, LinkedIn text)."""
    blocks: list[dict[str, object]] = []
    for title, text in lead.sections:
        body = text.strip()
        if not body:
            continue
        blocks.append(_heading(title))
        blocks.extend(_paragraph(chunk) for chunk in _chunks(body, _PARAGRAPH_LIMIT))
    return blocks[:_MAX_BODY_BLOCKS]


def event_blocks(lead: SalesLead, when: datetime) -> list[dict[str, object]]:
    """A short "what happened" note appended when an existing page is advanced."""
    if not lead.event:
        return []
    return [_paragraph(f"{when.strftime('%d.%m.%Y %H:%M')} UTC — {lead.event}")]


@dataclass(frozen=True, slots=True)
class LeadPage:
    """A Sales Pipeline page as the client sees it."""

    page_id: str
    url: str
    status: str | None = None


class NotionClient:
    """Reads and writes Sales Pipeline pages via the Notion REST API (no SDK)."""

    def __init__(self, config: NotionConfig, *, timeout: float = 15.0) -> None:
        self._config = config
        self._timeout = timeout

    async def find_by_link(self, url: str) -> LeadPage | None:
        """The existing pipeline entry for ``url``, if the project was already filed."""
        payload = {
            "filter": {"property": PROP_LINK, "url": {"equals": url}},
            "page_size": 1,
        }
        data = await self._request("POST", f"/databases/{self._config.database_id}/query", payload)
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return None
        first = results[0]
        return _to_lead_page(first) if isinstance(first, dict) else None

    async def create(self, lead: SalesLead) -> LeadPage:
        """File ``lead`` as a new page, body included."""
        payload: dict[str, object] = {
            "parent": {"database_id": self._config.database_id},
            "properties": lead_properties(lead),
        }
        blocks = lead_blocks(lead)
        if blocks:
            payload["children"] = blocks
        return _to_lead_page(await self._request("POST", "/pages", payload))

    async def advance(self, page: LeadPage, lead: SalesLead, *, now: datetime) -> LeadPage:
        """Move an existing entry forward and append what happened to its body."""
        properties = advance_properties(lead, page.status)
        if properties:
            await self._request("PATCH", f"/pages/{page.page_id}", {"properties": properties})
        blocks = event_blocks(lead, now)
        if blocks:
            await self._request("PATCH", f"/blocks/{page.page_id}/children", {"children": blocks})
        return page

    async def _request(
        self, method: str, path: str, payload: dict[str, object]
    ) -> dict[str, object]:
        headers = {
            "Authorization": f"Bearer {self._config.token}",
            "Notion-Version": NOTION_VERSION,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                response = await http.request(
                    method, f"{NOTION_API}{path}", json=payload, headers=headers
                )
        except httpx.HTTPError as err:
            raise NotionError(f"Notion request failed: {err}") from err
        if response.status_code != 200:
            raise NotionError(
                f"Notion rejected the request (HTTP {response.status_code}): {response.text[:300]}"
            )
        data: object = response.json()
        if not isinstance(data, dict):
            raise NotionError("Notion returned an unexpected response shape")
        return data


def _to_lead_page(data: dict[str, object]) -> LeadPage:
    page_id = data.get("id")
    url = data.get("url")
    return LeadPage(
        page_id=page_id if isinstance(page_id, str) else "",
        url=url if isinstance(url, str) else "",
        status=_status_of(data),
    )


def _status_of(data: dict[str, object]) -> str | None:
    """Read the ``Status`` option name out of a page payload (absent → ``None``)."""
    properties = data.get("properties")
    if not isinstance(properties, dict):
        return None
    status = properties.get(PROP_STATUS)
    if not isinstance(status, dict):
        return None
    value = status.get("status")
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    return name if isinstance(name, str) else None


class LeadStore(Protocol):
    """The Notion surface the service needs (``NotionClient`` satisfies it)."""

    async def find_by_link(self, url: str) -> LeadPage | None: ...
    async def create(self, lead: SalesLead) -> LeadPage: ...
    async def advance(self, page: LeadPage, lead: SalesLead, *, now: datetime) -> LeadPage: ...


type MatchMessageBuilder = Callable[[Listing, datetime], MatchMessage]


@dataclass(frozen=True, slots=True)
class FiledLead:
    """The outcome of one filing: where it landed and whether it was new."""

    url: str
    created: bool


class NotionSalesPipeline:
    """Files listings and applications as Sales Pipeline leads (upsert by listing link)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: LeadStore,
        build_message: MatchMessageBuilder,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._build_message = build_message

    async def add_listing(self, listing_id: int) -> FiledLead:
        """File a stored listing (the match message's Notion button)."""
        async with session_scope(self._session_factory) as session:
            listing = await Repository(session).get_listing_with_evaluations(listing_id)
            if listing is None:
                raise NotionError(f"Project {listing_id} not found")
            lead = lead_from_match(self._build_message(listing, _utcnow()))
        # The API calls happen outside the unit of work: a slow Notion response must
        # not hold a database transaction open.
        return await self._file(lead)

    async def add_application(self, application_id: int) -> FiledLead:
        """File an application draft or a sent application (button, and auto on send)."""
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
        return await self._file(lead)

    async def _file(self, lead: SalesLead) -> FiledLead:
        """Upsert ``lead``: advance the project's existing entry, or create a new one."""
        existing = await self._client.find_by_link(lead.url) if lead.url else None
        if existing is not None:
            page = await self._client.advance(existing, lead, now=_utcnow())
            return FiledLead(url=page.url, created=False)
        page = await self._client.create(lead)
        return FiledLead(url=page.url, created=True)

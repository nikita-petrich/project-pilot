"""Notifier-agnostic display data for a matched listing, and its builder.

Kept separate from any transport so the data shape survives when a
delivery backend is swapped out. ``to_match_message`` turns a stored or freshly
parsed listing plus an LLM verdict into the display-ready shape; it is shared by
the scan pipeline and the manual ``/check`` flow.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from project_pilot.ingestion.normalize import (
    detect_language,
    is_onsite_only,
    resolve_contact_name,
)
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import Listing


@dataclass(frozen=True, slots=True)
class MatchMessage:
    """Display-ready fields for one matched listing (all values pre-formatted)."""

    title: str
    url: str
    score: int
    # The database id, when the listing is stored. It is what a match thread needs
    # to reach the MCP tools (get_listing, draft_application); a freshly parsed
    # listing from a manual check has none yet.
    listing_id: int | None = None
    company: str | None = None
    contact_name: str | None = None
    is_endcustomer: bool | None = None
    location: str | None = None
    remote_label: str | None = None
    contract_type: str | None = None
    workload_label: str | None = None
    duration_label: str | None = None
    start: str | None = None
    posted_ago: str | None = None
    expires_label: str | None = None
    industry: str | None = None
    language: str | None = None
    skills: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    matching_skills: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    description: str = ""
    onsite_only: bool = False


_CONTRACT_LABELS = {
    "contracting": "Freelance",
    "contractor": "Freelance",
    "freelance": "Freelance",
    "employee_leasing": "Employee leasing",
    "permanent_position": "Permanent position",
    "temporary_employment": "Temporary employment",
}
_LANGUAGE_LABELS = {"de": "German", "en": "English"}


class _RawContract(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    contract_type: str | None = Field(default=None, alias="contractType")
    remote_in_percent: int | None = Field(default=None, alias="remoteInPercent")


class _RawIndustry(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name_de: str | None = Field(default=None, alias="nameDe")


class _RawFields(BaseModel):
    """The subset of the stored raw source record used to enrich a match message."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    company: str | None = None
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    workload: int | None = None
    duration_in_months: int | None = Field(default=None, alias="durationInMonths")
    duration_text: str | None = Field(default=None, alias="durationText")
    expires: str | None = None
    extension_possible: bool | None = Field(default=None, alias="extensionPossible")
    is_endcustomer_project: bool | None = Field(default=None, alias="isEndcustomerProject")
    industry: _RawIndustry | None = None
    contract: _RawContract | None = Field(default=None, alias="contractType")


def _relative_ago(posted_at: datetime, now: datetime) -> str | None:
    minutes = int((now - posted_at).total_seconds() // 60)
    if minutes < 0:
        return None
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 1440:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} d ago"


def _expires_label(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return None


def to_match_message(
    listing: Listing | ParsedListing,
    now: datetime,
    *,
    score: int,
    reasons: list[str],
    matching_skills: list[str],
    missing_requirements: list[str],
    risk_flags: list[str],
) -> MatchMessage:
    """Build the display shape from a listing (stored entity or freshly parsed)."""
    raw = _RawFields.model_validate(listing.raw or {})

    if listing.start_asap:
        start: str | None = "ASAP"
    elif listing.start_date is not None:
        start = listing.start_date.strftime("%d.%m.%Y")
    else:
        start = None

    # remoteInPercent == 0 is freelancermap's "not specified" default (often wrong for
    # agency posts), so only show it when it carries a real signal; the location line
    # still conveys remote/onsite. Genuine hybrids (1..99) show the on-site share.
    remote_pct = raw.contract.remote_in_percent if raw.contract else None
    if remote_pct is None or remote_pct <= 0:
        remote_label: str | None = None
    elif remote_pct >= 100:
        remote_label = "100%"
    else:
        remote_label = f"{remote_pct}% ({100 - remote_pct}% on-site)"

    contract_type = None
    if raw.contract and raw.contract.contract_type:
        contract_type = _CONTRACT_LABELS.get(raw.contract.contract_type, raw.contract.contract_type)

    duration_label = raw.duration_text or (
        f"{raw.duration_in_months} mo" if raw.duration_in_months else None
    )
    if duration_label and raw.extension_possible:
        duration_label += " (+ extension)"

    contact_name = resolve_contact_name(
        raw.first_name, raw.last_name, raw.company, listing.description or ""
    )

    language = detect_language(listing.description or listing.title)

    return MatchMessage(
        title=listing.title,
        url=listing.external_url,
        score=score,
        listing_id=listing.id if isinstance(listing, Listing) else None,
        company=raw.company,
        contact_name=contact_name,
        is_endcustomer=raw.is_endcustomer_project,
        location=listing.location,
        remote_label=remote_label,
        contract_type=contract_type,
        workload_label=f"{raw.workload}%" if raw.workload else None,
        duration_label=duration_label,
        start=start,
        posted_ago=_relative_ago(listing.posted_at, now) if listing.posted_at else None,
        expires_label=_expires_label(raw.expires),
        industry=raw.industry.name_de if raw.industry else None,
        language=_LANGUAGE_LABELS.get(language) if language else None,
        skills=list(listing.skills or []),
        reasons=reasons,
        matching_skills=matching_skills,
        missing_requirements=missing_requirements,
        risk_flags=risk_flags,
        description=listing.description or "",
        onsite_only=is_onsite_only(remote_pct, listing.location, listing.description or ""),
    )


# The compact match card, ported verbatim from the Slack notifier it replaced:
# who is hiring, where, the terms, and the top reasons — four scannable lines.
# It lives here rather than in a transport, because the routine fire, a future
# channel and any test all want the same layout.
_CARD_REASONS = 2
_CARD_RISKS = 2


def _inline(parts: Sequence[str | None]) -> str | None:
    """Join the given values into one middot-separated line, dropping the empty ones."""
    picked = [part for part in parts if part]
    return "  ·  ".join(picked) if picked else None


def _client_type(message: MatchMessage) -> str | None:
    if message.is_endcustomer is None:
        return None
    return "Direct client" if message.is_endcustomer else "Agency"


def _labeled_list(label: str, values: Sequence[str], *, limit: int) -> str | None:
    picked = [value for value in values if value][:limit]
    return f"{label}: {', '.join(picked)}" if picked else None


def headline(message: MatchMessage) -> str:
    """One line naming the match: score, role, company.

    Doubles as the session's name in the feed, so it stays short and puts the
    score first — that is what decides whether a listing is worth opening.
    """
    parts = [f"⭐ {message.score}", message.title]
    if message.company:
        parts.append(message.company)
    return " · ".join(parts)


def render_match_card(message: MatchMessage) -> str:
    """The at-a-glance overview of one match: headline, facts, fit, link.

    Company and location are always rendered, even when the listing names
    neither — an agency post that hides its client is itself a signal, so the
    card says so rather than silently dropping the line.
    """
    company = f"🏢 {message.company}" if message.company else "🏢 Company not stated"
    location = f"📍 {message.location}" if message.location else "📍 Location not stated"
    lines = [
        f"🎯 {message.title}  ·  {message.score}/100",
        _inline([company, _client_type(message)]),
        _inline([location, f"🏠 {message.remote_label}" if message.remote_label else None]),
        _inline(
            [
                f"📅 {message.start}" if message.start else None,
                f"⏳ {message.duration_label}" if message.duration_label else None,
                f"📊 {message.workload_label}" if message.workload_label else None,
                f"🕒 {message.posted_ago}" if message.posted_ago else None,
            ]
        ),
        _labeled_list("✅ Fits", message.reasons, limit=_CARD_REASONS),
        _labeled_list("⚠️ Risks", message.risk_flags, limit=_CARD_RISKS),
        f"🔗 {message.url}" if message.url else None,
    ]
    return "\n".join(line for line in lines if line)

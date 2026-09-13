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

from project_pilot.enrichment.links import (
    google_search_url,
    linkedin_company_url,
    linkedin_people_url,
)
from project_pilot.ingestion.normalize import (
    BERLIN,
    detect_language,
    is_onsite_only,
    resolve_contact_name,
)
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import Evaluation, EvaluationStage, Listing, Verdict


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
    # The listing's own database coordinates, so a chat can name the row it is
    # working on without a lookup. Only a stored listing has them.
    source: str | None = None
    origin: str | None = None
    status: str | None = None
    # The score's yardstick (MATCH_THRESHOLD): 87 says little without the 60.
    threshold: int | None = None
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
    # The absolute posting time in Berlin display time. ``posted_ago`` is read
    # minutes after the alert; a chat opened hours later needs the real clock.
    posted_at_label: str | None = None
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


def _berlin_label(value: datetime | None) -> str | None:
    """The absolute timestamp in Berlin display time; storage stays UTC."""
    return value.astimezone(BERLIN).strftime("%d.%m.%Y %H:%M") if value else None


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
    threshold: int | None = None,
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
        source=listing.source,
        origin=listing.origin.value if isinstance(listing, Listing) else None,
        status=listing.status.value if isinstance(listing, Listing) else None,
        threshold=threshold,
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
        posted_at_label=_berlin_label(listing.posted_at),
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


# The match message body, ported from the Slack notifier it replaced: every
# listing fact on its own labelled line, then the verdict. The description is
# not here — it goes behind its own button, because a listing text can be
# thousands of characters and would push the facts out of the first screen.
# This lives here rather than in a transport, because any channel and any test
# wants the same layout.
_FACT_SKILLS = 12
_VERDICT_REASONS = 3
_VERDICT_MATCHING_SKILLS = 8
_VERDICT_GAPS = 4
_VERDICT_RISKS = 3


def _client_type(message: MatchMessage) -> str | None:
    if message.is_endcustomer is None:
        return None
    return "Direct client" if message.is_endcustomer else "Agency"


def _labeled(label: str, value: str | None) -> str | None:
    return f"{label}: {value}" if value else None


def _labeled_list(label: str, values: Sequence[str], *, limit: int) -> str | None:
    picked = [value for value in values if value][:limit]
    return f"{label}: {', '.join(picked)}" if picked else None


def headline(message: MatchMessage) -> str:
    """One line naming the match: score, role, company.

    Doubles as the topic's name in the group, so it stays short and puts the
    score first — that is what decides whether a listing is worth opening.
    """
    parts = [f"⭐ {message.score}", message.title]
    if message.company:
        parts.append(message.company)
    return " · ".join(parts)


def render_match_details(message: MatchMessage) -> str:
    """Every listing fact and the full verdict, one labelled line each.

    Company, location and industry are rendered even when the listing names
    none of them — an agency post that hides its client is itself a signal, so
    the message says so rather than silently dropping the line.
    """
    facts = [
        _labeled("🏢 Company", message.company or "not stated"),
        _labeled("👤 Contact", message.contact_name),
        _labeled("🤝 Client type", _client_type(message)),
        _labeled("📍 Location", message.location or "not stated"),
        _labeled("🏠 Remote", message.remote_label),
        _labeled("💼 Contract", message.contract_type),
        _labeled("📊 Workload", message.workload_label),
        _labeled("⏳ Duration", message.duration_label),
        _labeled("📅 Start", message.start),
        _labeled("🕒 Posted", message.posted_ago),
        _labeled("✍️ Apply by", message.expires_label),
        _labeled("🏭 Industry", message.industry or "unknown"),
        _labeled("🗣 Language", message.language),
        _labeled_list("🛠 Skills", message.skills, limit=_FACT_SKILLS),
    ]
    verdict = [
        f"🎯 Score: {message.score}/100",
        _labeled_list("✅ Fits", message.reasons, limit=_VERDICT_REASONS),
        _labeled_list("🎯 Your skills", message.matching_skills, limit=_VERDICT_MATCHING_SKILLS),
        _labeled_list("⚠️ Gaps", message.missing_requirements, limit=_VERDICT_GAPS),
        _labeled_list("🚩 Risks", message.risk_flags, limit=_VERDICT_RISKS),
    ]
    blocks = [
        "\n".join(line for line in facts if line),
        "\n".join(line for line in verdict if line),
    ]
    if message.url:
        blocks.append(f"🔗 {message.url}")
    return "\n\n".join(block for block in blocks if block)


def _latest_match_evaluation(listing: Listing) -> Evaluation | None:
    """The newest LLM verdict on a listing, which is the one that decided it."""
    matches = [
        evaluation
        for evaluation in listing.evaluations
        if evaluation.stage is EvaluationStage.LLM and evaluation.verdict is Verdict.MATCH
    ]
    if not matches:
        return None
    return max(matches, key=lambda evaluation: evaluation.created_at)


def _eval_list(evaluation: Evaluation | None, key: str) -> list[str]:
    if evaluation is None:
        return []
    value = evaluation.reason.get(key)
    return [str(item) for item in value] if isinstance(value, list) else []


def from_stored(listing: Listing, now: datetime, *, threshold: int | None = None) -> MatchMessage:
    """The display shape for a listing already judged and stored.

    Reads the verdict off the listing's own evaluations rather than asking the
    LLM again, so showing a match a second time costs nothing.
    """
    evaluation = _latest_match_evaluation(listing)
    score = evaluation.score if evaluation is not None and evaluation.score is not None else 0
    return to_match_message(
        listing,
        now,
        score=score,
        reasons=_eval_list(evaluation, "reasons"),
        matching_skills=_eval_list(evaluation, "matching_skills"),
        missing_requirements=_eval_list(evaluation, "missing_requirements"),
        risk_flags=_eval_list(evaluation, "risk_flags"),
        threshold=threshold,
    )


# Stated rather than dropped, like the card's own "not stated" lines: an ad with
# no named contact is a fact worth seeing, not an absent row to puzzle over.
NO_CONTACT = "K.A."


def research_lines(message: MatchMessage) -> list[str]:
    """The lines the card has no room for: DB coordinates and research links.

    Every one of them is a thing Nik would otherwise look up by hand — the row
    id he has to name to any tool, when the ad really went up, and the two
    LinkedIn searches plus the Impressum query he opens before writing.
    """
    lines: list[str] = []
    if message.listing_id is not None:
        coordinates = [
            f"listing_id {message.listing_id}",
            *(part for part in (message.source, message.origin, message.status) if part),
        ]
        lines.append(f"🗄 DB: {' · '.join(coordinates)}")
    if message.threshold is not None:
        reached = "erreicht" if message.score >= message.threshold else "verfehlt"
        lines.append(f"📏 Schwelle: {message.threshold} ({reached})")
    if message.posted_at_label:
        lines.append(f"🗓 Eingestellt: {message.posted_at_label} (Berlin)")
    if message.onsite_only:
        lines.append("🚨 Liest sich als reines Vor-Ort-Projekt — vor dem Senden prüfen.")
    if message.company:
        lines.append(f"👥 LinkedIn Firma: {linkedin_company_url(message.company)}")
        lines.append(
            f"🔎 Impressum/Kontakt: "
            f"{google_search_url(f'{message.company} Impressum Kontakt E-Mail')}"
        )
    person = (
        linkedin_people_url(company=message.company, person=message.contact_name)
        if message.contact_name
        else NO_CONTACT
    )
    lines.append(f"🙋 LinkedIn Person: {person}")
    return lines


def render_card(message: MatchMessage) -> str:
    """The whole overview: headline, every fact, the verdict, then the research links.

    One definition for three surfaces — the Telegram card shows the first two
    blocks, the chat prompt names the listing, and the MCP ``match_card`` tool
    hands this back verbatim so a skill can print it without knowing the layout.
    """
    return "\n\n".join(
        [
            headline(message),
            render_match_details(message),
            "\n".join(research_lines(message)),
        ]
    )

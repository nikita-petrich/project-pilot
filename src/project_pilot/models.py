"""SQLAlchemy 2.0 ORM entities: listings, evaluations, runs, source_state."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class RemoteStatus(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class PostedPrecision(StrEnum):
    MINUTE = "minute"
    DAY = "day"
    UNKNOWN = "unknown"


class ListingStatus(StrEnum):
    NEW = "new"
    EVALUATED = "evaluated"
    SKIPPED_STALE = "skipped_stale"


class EvaluationStage(StrEnum):
    FRESHNESS = "freshness"
    HARD_RULE = "hard_rule"
    LLM = "llm"


class Verdict(StrEnum):
    MATCH = "match"
    NO_MATCH = "no_match"
    SKIPPED_STALE = "skipped_stale"


class RunStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"


class ApplicationStatus(StrEnum):
    AWAITING_EMAIL = "awaiting_email"
    READY = "ready"
    SENDING = "sending"
    SENT = "sent"
    CANCELLED = "cancelled"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """A native PostgreSQL enum whose labels are the members' string values."""

    def values(cls: type[StrEnum]) -> Sequence[str]:
        return [member.value for member in cls]

    return Enum(enum_cls, name=name, values_callable=values)


class Base(DeclarativeBase):
    pass


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    external_url: Mapped[str] = mapped_column(String(1024), unique=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list[str]] = mapped_column(JSONB, default=list)

    start_date: Mapped[date | None] = mapped_column(Date, default=None)
    start_asap: Mapped[bool] = mapped_column(default=False)
    end_date: Mapped[date | None] = mapped_column(Date, default=None)

    location: Mapped[str | None] = mapped_column(String(256), default=None)
    remote_status: Mapped[RemoteStatus] = mapped_column(
        _pg_enum(RemoteStatus, "remote_status"), default=RemoteStatus.UNKNOWN
    )

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    posted_at_precision: Mapped[PostedPrecision] = mapped_column(
        _pg_enum(PostedPrecision, "posted_at_precision"), default=PostedPrecision.UNKNOWN
    )

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    status: Mapped[ListingStatus] = mapped_column(
        _pg_enum(ListingStatus, "listing_status"), default=ListingStatus.NEW
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # The Claude match-thread session opened for this listing (feature 22); also
    # the double-fire guard, since the routine fire endpoint has no idempotency key.
    claude_session_url: Mapped[str | None] = mapped_column(String(512), default=None)

    raw: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)

    evaluations: Mapped[list["Evaluation"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class Evaluation(Base):
    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[EvaluationStage] = mapped_column(_pg_enum(EvaluationStage, "evaluation_stage"))
    verdict: Mapped[Verdict] = mapped_column(_pg_enum(Verdict, "verdict"))
    score: Mapped[int | None] = mapped_column(default=None)
    reason: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)

    model: Mapped[str | None] = mapped_column(String(128), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(64), default=None)
    profile_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    tokens_in: Mapped[int | None] = mapped_column(default=None)
    tokens_out: Mapped[int | None] = mapped_column(default=None)
    latency_ms: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    listing: Mapped["Listing"] = relationship(back_populates="evaluations")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[RunStatus] = mapped_column(
        _pg_enum(RunStatus, "run_status"), default=RunStatus.SUCCESS
    )
    fetched: Mapped[int] = mapped_column(default=0)
    new: Mapped[int] = mapped_column(default=0)
    evaluated: Mapped[int] = mapped_column(default=0)
    matched: Mapped[int] = mapped_column(default=0)
    notified: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class Application(Base):
    """One application draft/send cycle for a listing (or an ad-hoc `/apply` text)."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL"), index=True, default=None
    )
    listing_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    listing_title: Mapped[str] = mapped_column(String(512))
    listing_text: Mapped[str] = mapped_column(Text, default="")
    contact_name: Mapped[str | None] = mapped_column(String(256), default=None)
    company: Mapped[str | None] = mapped_column(String(256), default=None)

    recipient_email: Mapped[str | None] = mapped_column(String(320), default=None)
    subject: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    linkedin_message: Mapped[str] = mapped_column(String(300), default="")

    status: Mapped[ApplicationStatus] = mapped_column(
        _pg_enum(ApplicationStatus, "application_status"), default=ApplicationStatus.READY
    )
    draft_ref: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    revision_count: Mapped[int] = mapped_column(default=0)

    model: Mapped[str | None] = mapped_column(String(128), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(64), default=None)
    profile_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    tokens_in: Mapped[int | None] = mapped_column(default=None)
    tokens_out: Mapped[int | None] = mapped_column(default=None)

    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ContactLead(Base):
    """Contact data found for a listing's company (Impressum/website + research links).

    Additive, append-only record for traceability; one row per enrichment lookup so a
    company can be re-checked later without losing the earlier result.
    """

    __tablename__ = "contact_leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL"), index=True, default=None
    )
    company: Mapped[str | None] = mapped_column(String(512), default=None)
    person: Mapped[str | None] = mapped_column(String(256), default=None)
    website: Mapped[str | None] = mapped_column(String(1024), default=None)
    emails: Mapped[list[str]] = mapped_column(JSONB, default=list)
    phones: Mapped[list[str]] = mapped_column(JSONB, default=list)
    persons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sources: Mapped[list[str]] = mapped_column(JSONB, default=list)
    links: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    linkedin_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SourceState(Base):
    __tablename__ = "source_state"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    consecutive_failures: Mapped[int] = mapped_column(default=0)

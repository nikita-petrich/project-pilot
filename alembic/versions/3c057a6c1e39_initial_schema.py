"""initial schema

Revision ID: 3c057a6c1e39
Revises:
Create Date: 2026-07-21 09:09:36.126692

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c057a6c1e39"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_TYPES = (
    "remote_status",
    "posted_at_precision",
    "listing_status",
    "run_status",
    "evaluation_stage",
    "verdict",
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "listings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_url", sa.String(length=1024), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("start_asap", sa.Boolean(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("location", sa.String(length=256), nullable=True),
        sa.Column(
            "remote_status",
            sa.Enum("remote", "hybrid", "onsite", "unknown", name="remote_status"),
            nullable=False,
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "posted_at_precision",
            sa.Enum("minute", "day", "unknown", name="posted_at_precision"),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum("new", "evaluated", "skipped_stale", name="listing_status"),
            nullable=False,
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_url"),
    )
    op.create_index(op.f("ix_listings_url_hash"), "listings", ["url_hash"], unique=True)
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("success", "partial", "error", name="run_status"),
            nullable=False,
        ),
        sa.Column("fetched", sa.Integer(), nullable=False),
        sa.Column("new", sa.Integer(), nullable=False),
        sa.Column("evaluated", sa.Integer(), nullable=False),
        sa.Column("matched", sa.Integer(), nullable=False),
        sa.Column("notified", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "source_state",
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("source"),
    )
    op.create_table(
        "evaluations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column(
            "stage",
            sa.Enum("freshness", "hard_rule", "llm", name="evaluation_stage"),
            nullable=False,
        ),
        sa.Column(
            "verdict",
            sa.Enum("match", "no_match", "skipped_stale", name="verdict"),
            nullable=False,
        ),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("reason", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("profile_hash", sa.String(length=64), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_evaluations_listing_id"), "evaluations", ["listing_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_evaluations_listing_id"), table_name="evaluations")
    op.drop_table("evaluations")
    op.drop_table("source_state")
    op.drop_table("runs")
    op.drop_index(op.f("ix_listings_url_hash"), table_name="listings")
    op.drop_table("listings")
    for enum_name in _ENUM_TYPES:
        op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))

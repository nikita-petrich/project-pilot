"""add applications table

Revision ID: 9f2d41b7a3c8
Revises: 3c057a6c1e39
Create Date: 2026-07-23 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f2d41b7a3c8"
down_revision: str | Sequence[str] | None = "3c057a6c1e39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=True),
        sa.Column("listing_url", sa.String(length=1024), nullable=True),
        sa.Column("listing_title", sa.String(length=512), nullable=False),
        sa.Column("listing_text", sa.Text(), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("linkedin_message", sa.String(length=300), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "awaiting_email",
                "ready",
                "sending",
                "sent",
                "cancelled",
                name="application_status",
            ),
            nullable=False,
        ),
        sa.Column("draft_message_id", sa.BigInteger(), nullable=True),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("profile_hash", sa.String(length=64), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_applications_listing_id"), "applications", ["listing_id"], unique=False
    )
    op.create_index(
        op.f("ix_applications_draft_message_id"),
        "applications",
        ["draft_message_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_applications_draft_message_id"), table_name="applications")
    op.drop_index(op.f("ix_applications_listing_id"), table_name="applications")
    op.drop_table("applications")
    op.execute(sa.text("DROP TYPE IF EXISTS application_status"))

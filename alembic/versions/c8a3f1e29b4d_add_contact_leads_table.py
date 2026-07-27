"""add contact_leads table

Stores contact data found for a listing's company (e-mails, phones, contact
persons, website, and the LinkedIn/Google research links) for traceability.

Revision ID: c8a3f1e29b4d
Revises: b7e4c2f10a5d
Create Date: 2026-07-27 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c8a3f1e29b4d"
down_revision: str | Sequence[str] | None = "b7e4c2f10a5d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "contact_leads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=True),
        sa.Column("company", sa.String(length=512), nullable=True),
        sa.Column("person", sa.String(length=256), nullable=True),
        sa.Column("website", sa.String(length=1024), nullable=True),
        sa.Column("emails", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("phones", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("persons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("links", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_contact_leads_listing_id"), "contact_leads", ["listing_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_contact_leads_listing_id"), table_name="contact_leads")
    op.drop_table("contact_leads")

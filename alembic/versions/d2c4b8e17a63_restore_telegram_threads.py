"""Restore telegram_threads.

Feature 26 had moved the conversation out of Telegram and dropped this table
with it; the match thread is back, so the mapping listing -> forum topic and
the topic's history need their table again. Re-created forward rather than by
reverting f3a7d195c204, because production already ran that drop: its
``alembic_version`` names it, and deleting the file would leave the schema
pointing at a revision that no longer exists.

Revision ID: d2c4b8e17a63
Revises: f3a7d195c204
Create Date: 2026-08-26 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d2c4b8e17a63"
down_revision: str | Sequence[str] | None = "f3a7d195c204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "telegram_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("history", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id"),
    )
    op.create_index("ix_telegram_threads_thread_id", "telegram_threads", ["thread_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_telegram_threads_thread_id", table_name="telegram_threads")
    op.drop_table("telegram_threads")

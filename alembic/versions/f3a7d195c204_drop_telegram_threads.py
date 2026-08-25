"""Drop telegram_threads.

The Telegram side no longer holds a conversation: a match is one message with
buttons, and the conversation happens in the Claude project. With no thread to
route replies into and no history to keep, the table has nothing left to store.

Revision ID: f3a7d195c204
Revises: e91d4c62b7a3
Create Date: 2026-08-24 21:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f3a7d195c204"
down_revision: str | Sequence[str] | None = "e91d4c62b7a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("ix_telegram_threads_thread_id", table_name="telegram_threads")
    op.drop_table("telegram_threads")


def downgrade() -> None:
    """Downgrade schema."""
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

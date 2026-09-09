"""Claude session per match; Telegram is an alert again.

A match is worked in its own Claude cloud session, opened by the worker through
the routine's fire endpoint. The session's URL lives on the listing: the MCP
feed links to it, and the pipeline never fires twice for the same listing (the
fire endpoint has no idempotency key).

Telegram carries the alert and three buttons, nothing more. There is no thread
to route replies into and no agent session to continue, so ``telegram_threads``
has nothing left to store.

Revision ID: e5b7c3d9a1f4
Revises: c9e2a45b8d31
Create Date: 2026-09-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b7c3d9a1f4"
down_revision: str | Sequence[str] | None = "c9e2a45b8d31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("listings", sa.Column("claude_session_url", sa.String(length=512), nullable=True))
    op.drop_index("ix_telegram_threads_channel_message_id", table_name="telegram_threads")
    op.drop_index("ix_telegram_threads_thread_id", table_name="telegram_threads")
    op.drop_table("telegram_threads")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "telegram_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=True),
        sa.Column("channel_message_id", sa.BigInteger(), nullable=True),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id"),
    )
    op.create_index("ix_telegram_threads_thread_id", "telegram_threads", ["thread_id"], unique=True)
    op.create_index(
        "ix_telegram_threads_channel_message_id",
        "telegram_threads",
        ["channel_message_id"],
        unique=True,
    )
    op.drop_column("listings", "claude_session_url")

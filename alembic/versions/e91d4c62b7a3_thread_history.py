"""Add the conversation held in a match topic.

The Telegram thread agent (feature 25b) answers in the topic, so each topic
carries its own conversation. Stored as plain text turns rather than API
content blocks: the MCP tools are the source of truth and every turn re-reads
the state from the database, which keeps this column small and independent of
the API's block format.

Revision ID: e91d4c62b7a3
Revises: b5f2c81ae934
Create Date: 2026-08-24 20:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e91d4c62b7a3"
down_revision: str | Sequence[str] | None = "b5f2c81ae934"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "telegram_threads",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.alter_column("telegram_threads", "updated_at", server_default=None)
    op.add_column(
        "telegram_threads",
        sa.Column("history", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.alter_column("telegram_threads", "history", server_default=None)
    # Incoming messages are routed by thread_id, so it needs its own index.
    op.create_index("ix_telegram_threads_thread_id", "telegram_threads", ["thread_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_telegram_threads_thread_id", table_name="telegram_threads")
    op.drop_column("telegram_threads", "history")
    op.drop_column("telegram_threads", "updated_at")

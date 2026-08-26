"""Trade the topic's stored history for the agent session it continues in.

The thread agent now runs on the Claude Agent SDK, which keeps the transcript
itself. Storing turns here as well would mean two copies of one conversation
with no rule for reconciling them, so the column goes and the session id takes
its place.

Revision ID: a7f4c92d31e8
Revises: d2c4b8e17a63
Create Date: 2026-08-26 11:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7f4c92d31e8"
down_revision: str | Sequence[str] | None = "d2c4b8e17a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("telegram_threads", sa.Column("session_id", sa.String(64), nullable=True))
    op.drop_column("telegram_threads", "history")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "telegram_threads",
        sa.Column("history", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.drop_column("telegram_threads", "session_id")

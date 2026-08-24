"""Add telegram_threads: the forum topic a match got.

Telegram's ``message_thread_id`` is the handle for the topic opened per match —
used to send into it now, and to route an incoming message back to its listing
later. ``listing_id`` is unique so a repeated run cannot open a second topic for
the same project.

Revision ID: b5f2c81ae934
Revises: c7e41b93da05
Create Date: 2026-08-24 19:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5f2c81ae934"
down_revision: str | Sequence[str] | None = "c7e41b93da05"
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
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("telegram_threads")

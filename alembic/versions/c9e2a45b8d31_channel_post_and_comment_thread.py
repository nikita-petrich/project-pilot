"""Channel post and comment thread on telegram_threads

Revision ID: c9e2a45b8d31
Revises: b6d3e18f47a2
Create Date: 2026-08-28

A match is no longer a forum topic the bot opens: it is a post in a channel,
whose comment thread Telegram creates by forwarding the post into the linked
discussion group. That is two ids in two chats. ``channel_message_id`` is known
when the card is sent; ``thread_id`` — the forwarded copy's id in the group — is
only known when the automatic forward comes back through getUpdates, so it has
to be nullable now.

Rows written under the forum-topic design name topics that no longer exist, so
they are cleared rather than migrated: their ids would address nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e2a45b8d31"
down_revision: str | None = "b6d3e18f47a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM telegram_threads")
    op.add_column(
        "telegram_threads",
        sa.Column("channel_message_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_telegram_threads_channel_message_id",
        "telegram_threads",
        ["channel_message_id"],
        unique=True,
    )
    op.alter_column("telegram_threads", "thread_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    # A row without a thread id cannot exist under the old shape, and one that
    # has both ids still names a topic the forum design never created.
    op.execute("DELETE FROM telegram_threads")
    op.alter_column("telegram_threads", "thread_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_index("ix_telegram_threads_channel_message_id", table_name="telegram_threads")
    op.drop_column("telegram_threads", "channel_message_id")

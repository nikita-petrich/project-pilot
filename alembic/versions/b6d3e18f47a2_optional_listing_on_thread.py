"""Let a topic exist without a listing.

A thread a human opens is a conversation with the agent before it is about any
particular project — it may be a pasted description, a link, or a question. It
still needs a row, because that is where its session id lives, so the listing
becomes optional and the thread id becomes the identity.

Revision ID: b6d3e18f47a2
Revises: a7f4c92d31e8
Create Date: 2026-08-26 21:50:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d3e18f47a2"
down_revision: str | Sequence[str] | None = "a7f4c92d31e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("telegram_threads", "listing_id", existing_type=sa.Integer(), nullable=True)
    op.drop_index("ix_telegram_threads_thread_id", table_name="telegram_threads")
    op.create_index("ix_telegram_threads_thread_id", "telegram_threads", ["thread_id"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    # Rows without a listing cannot survive the column becoming NOT NULL again.
    op.execute(sa.text("DELETE FROM telegram_threads WHERE listing_id IS NULL"))
    op.drop_index("ix_telegram_threads_thread_id", table_name="telegram_threads")
    op.create_index("ix_telegram_threads_thread_id", "telegram_threads", ["thread_id"])
    op.alter_column("telegram_threads", "listing_id", existing_type=sa.Integer(), nullable=False)

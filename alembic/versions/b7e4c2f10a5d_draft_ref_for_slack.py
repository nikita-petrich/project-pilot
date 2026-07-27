"""draft reference as a Slack channel:ts string

Replaces the Telegram integer message id (draft_message_id) with a string
draft_ref that holds the Slack "channel:ts" of the draft message.

Revision ID: b7e4c2f10a5d
Revises: 9f2d41b7a3c8
Create Date: 2026-07-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e4c2f10a5d"
down_revision: str | Sequence[str] | None = "9f2d41b7a3c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(op.f("ix_applications_draft_message_id"), table_name="applications")
    op.drop_column("applications", "draft_message_id")
    op.add_column("applications", sa.Column("draft_ref", sa.String(length=128), nullable=True))
    op.create_index(op.f("ix_applications_draft_ref"), "applications", ["draft_ref"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_applications_draft_ref"), table_name="applications")
    op.drop_column("applications", "draft_ref")
    op.add_column("applications", sa.Column("draft_message_id", sa.BigInteger(), nullable=True))
    op.create_index(
        op.f("ix_applications_draft_message_id"),
        "applications",
        ["draft_message_id"],
        unique=False,
    )

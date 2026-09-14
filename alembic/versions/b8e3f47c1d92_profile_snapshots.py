"""Keep one copy of every profile a verdict was judged against.

The profile moved to the website, where it changes without a deploy, so the
``profile_hash`` stored on each evaluation and application would otherwise refer
to a text that exists nowhere any more. Each distinct profile state is written
here once, the first time it is used.

Revision ID: b8e3f47c1d92
Revises: f1c6b47d9e25
Create Date: 2026-09-14 12:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e3f47c1d92"
down_revision: str | Sequence[str] | None = "f1c6b47d9e25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "profile_snapshots",
        sa.Column("profile_hash", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("profile_hash"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("profile_snapshots")

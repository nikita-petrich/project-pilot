"""Drop listings.claude_session_url.

The column held the Claude session a routine fire had created per match. The
notification channel is now an ntfy push from the worker, whose click target is
one Claude project rather than a per-listing session, so nothing can fill this
column any more.

Revision ID: c7e41b93da05
Revises: a1c9e6b73f28
Create Date: 2026-08-24 03:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e41b93da05"
down_revision: str | Sequence[str] | None = "a1c9e6b73f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("listings", "claude_session_url")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("listings", sa.Column("claude_session_url", sa.String(length=512), nullable=True))

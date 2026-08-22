"""Add listings.claude_session_url.

Stores the Claude match-thread session created for a notified match, so the
MCP feed can link straight into the thread and the pipeline never fires the
routine twice for the same listing (the fire endpoint has no idempotency key).

Revision ID: d4e7f2a85b19
Revises: f6b8d3a91c72
Create Date: 2026-08-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e7f2a85b19"
down_revision: str | Sequence[str] | None = "f6b8d3a91c72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("listings", sa.Column("claude_session_url", sa.String(length=512), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("listings", "claude_session_url")

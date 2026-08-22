"""Add listings.origin — how a listing entered the database.

Everything the scanner fetched is `scan`, which is what the backfill sets for
every existing row and what the column defaults to. Listings that arrive through
the MCP `ingest_listing` tool name their channel instead (chat, mail, pdf,
image, url, api), so a stored listing always says where it came from.

Revision ID: a1c9e6b73f28
Revises: d4e7f2a85b19
Create Date: 2026-08-22 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c9e6b73f28"
down_revision: str | Sequence[str] | None = "d4e7f2a85b19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGIN = sa.Enum(
    "scan",
    "chat",
    "mail",
    "pdf",
    "image",
    "url",
    "api",
    name="listing_origin",
)


def upgrade() -> None:
    """Upgrade schema."""
    _ORIGIN.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "listings",
        sa.Column(
            "origin",
            _ORIGIN,
            nullable=False,
            server_default="scan",
        ),
    )
    # The server default exists only to fill the existing rows; the application
    # sets the value explicitly on every insert.
    op.alter_column("listings", "origin", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("listings", "origin")
    _ORIGIN.drop(op.get_bind(), checkfirst=True)

"""contact name on applications

Stores the resolved contact person of a draft so the Slack messages can offer
a LinkedIn people-search button next to every LinkedIn message.

Revision ID: e2a9c5d47f13
Revises: b7e4c2f10a5d
Create Date: 2026-07-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a9c5d47f13"
down_revision: str | Sequence[str] | None = "b7e4c2f10a5d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("applications", sa.Column("contact_name", sa.String(length=256), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("applications", "contact_name")

"""company on applications

Stores the listing's company for a draft so the LinkedIn people search can
combine "person AND company" instead of searching the bare name.

Revision ID: f6b8d3a91c72
Revises: a04d6abe61e5
Create Date: 2026-08-02 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6b8d3a91c72"
down_revision: str | Sequence[str] | None = "a04d6abe61e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("applications", sa.Column("company", sa.String(length=256), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("applications", "company")

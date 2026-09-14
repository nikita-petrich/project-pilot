"""Remember which Telegram message carries a listing's match card.

A card is a decision surface: once the match has been taken up, it has done its
job and should leave the feed. Telegram sends no update for a URL button, so the
**Bewerben** tap itself cannot be heard — the card is instead removed when the
chat it opens actually starts working the listing (a draft). That needs the id
of the message the worker sent, which is what this column holds.

Revision ID: f1c6b47d9e25
Revises: e5b7c3d9a1f4
Create Date: 2026-09-14 09:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1c6b47d9e25"
down_revision: str | Sequence[str] | None = "e5b7c3d9a1f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("listings", sa.Column("card_message_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("listings", "card_message_id")

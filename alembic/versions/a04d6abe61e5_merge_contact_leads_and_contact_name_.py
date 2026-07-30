"""Merge the contact_leads and contact_name heads.

Two features branched from ``b7e4c2f10a5d`` and were merged without an Alembic merge
revision, leaving the tree with two heads. Nothing surfaces that until ``upgrade head``
has to pick one and refuses — which is where the container's ``init-db`` crashed on
start.

The two branches are independent (one creates the ``contact_leads`` table, the other
adds ``applications.contact_name``), so joining them needs no schema change of its own.
A test now asserts a single head, so the quality gate catches the next fork before a
deploy does.

Revision ID: a04d6abe61e5
Revises: c8a3f1e29b4d, e2a9c5d47f13
Create Date: 2026-07-30 05:09:38.176762

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a04d6abe61e5"
down_revision: str | Sequence[str] | None = ("c8a3f1e29b4d", "e2a9c5d47f13")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join both branches; neither needs a schema change here."""


def downgrade() -> None:
    """Split back into the two heads; nothing to undo."""

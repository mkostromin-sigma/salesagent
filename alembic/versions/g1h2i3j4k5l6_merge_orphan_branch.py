"""Merge orphan branch into main

Revision ID: g1h2i3j4k5l6
Revises: f3bac4654620, 30acc1daf358
Create Date: 2026-01-01 21:30:00.000000

This migration merges the orphan branch (30acc1daf358) back into main.

The migration 30acc1daf358 was accidentally deleted from the codebase but
had already been applied to the production database. This merge migration
brings both branches back together into a single head.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "g1h2i3j4k5l6"
down_revision: str | Sequence[str] | None = ("f3bac4654620", "30acc1daf358")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

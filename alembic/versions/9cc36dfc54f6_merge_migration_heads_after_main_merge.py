"""merge migration heads after main merge

Revision ID: 9cc36dfc54f6
Revises: c3d10c6688d1, c612d0326eb0
Create Date: 2026-04-17 20:15:47.552788

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "9cc36dfc54f6"
down_revision: str | Sequence[str] | None = ("c3d10c6688d1", "c612d0326eb0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

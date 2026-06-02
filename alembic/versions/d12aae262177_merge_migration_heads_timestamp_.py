"""merge migration heads: timestamp defaults + account_approval_mode

Revision ID: d12aae262177
Revises: 529ae3fa444b, c612d0326eb0
Create Date: 2026-05-03 18:02:08.281805

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d12aae262177"
down_revision: str | Sequence[str] | None = ("529ae3fa444b", "c612d0326eb0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

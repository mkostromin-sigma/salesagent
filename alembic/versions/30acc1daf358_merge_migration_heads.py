"""Merge migration heads

Revision ID: 30acc1daf358
Revises: a2a336ecb71d, add_auth_setup_mode
Create Date: 2025-12-31 00:07:20.405890

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "30acc1daf358"
down_revision: str | Sequence[str] | None = ("a2a336ecb71d", "add_auth_setup_mode")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

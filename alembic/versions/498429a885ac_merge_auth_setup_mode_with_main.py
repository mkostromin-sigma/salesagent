"""merge_auth_setup_mode_with_main

Revision ID: 498429a885ac
Revises: add_auth_setup_mode
Create Date: 2025-12-31 00:32:14.947501

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "498429a885ac"
down_revision: str | Sequence[str] | None = ("add_auth_setup_mode", "a2a336ecb71d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

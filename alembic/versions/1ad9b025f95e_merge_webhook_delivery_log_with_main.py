"""merge webhook delivery log with main

Revision ID: 1ad9b025f95e
Revises: 1759f70fc76a, b79d575c2dcc
Create Date: 2025-11-18 19:49:00.734388

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "1ad9b025f95e"
down_revision: str | Sequence[str] | None = ("1759f70fc76a", "b79d575c2dcc")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

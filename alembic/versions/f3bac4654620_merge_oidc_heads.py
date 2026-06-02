"""merge_oidc_heads

Revision ID: f3bac4654620
Revises: 58e9d3fdf1f6, add_oidc_logout_url
Create Date: 2026-01-01 14:00:41.717523

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f3bac4654620"
down_revision: str | Sequence[str] | None = ("58e9d3fdf1f6", "add_oidc_logout_url")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

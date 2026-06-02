"""merge_heads

Revision ID: e4f26160e57b
Revises: 2b64218bfe0e, e8223bd175df
Create Date: 2025-11-25 10:43:41.902349

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "e4f26160e57b"
down_revision: str | Sequence[str] | None = ("2b64218bfe0e", "e8223bd175df")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

"""merge migration heads

Revision ID: 6ae57c13adca
Revises: 2b64218bfe0e, e8223bd175df
Create Date: 2025-11-25 19:30:29.569846

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "6ae57c13adca"
down_revision: str | Sequence[str] | None = ("2b64218bfe0e", "e8223bd175df")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

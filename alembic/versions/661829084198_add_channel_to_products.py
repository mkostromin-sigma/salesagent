"""add_channel_to_products

Revision ID: 661829084198
Revises: 6cc4765445a1
Create Date: 2025-12-03 05:04:40.927580

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "661829084198"
down_revision: str | Sequence[str] | None = "6cc4765445a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add channel column to products table."""
    op.add_column("products", sa.Column("channel", sa.String(50), nullable=True))


def downgrade() -> None:
    """Remove channel column from products table."""
    op.drop_column("products", "channel")

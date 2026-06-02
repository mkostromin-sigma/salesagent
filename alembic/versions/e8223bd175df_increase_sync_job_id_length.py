"""increase_sync_job_id_length

Revision ID: e8223bd175df
Revises: 445171389125
Create Date: 2025-11-24 17:59:03.448623

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8223bd175df"
down_revision: str | Sequence[str] | None = "445171389125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "sync_jobs", "sync_id", existing_type=sa.String(length=50), type_=sa.String(length=100), existing_nullable=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Note: This downgrade might fail if there are IDs longer than 50 chars
    # We generally don't support downgrading data-loss migrations, but for completeness:
    op.alter_column(
        "sync_jobs", "sync_id", existing_type=sa.String(length=100), type_=sa.String(length=50), existing_nullable=False
    )

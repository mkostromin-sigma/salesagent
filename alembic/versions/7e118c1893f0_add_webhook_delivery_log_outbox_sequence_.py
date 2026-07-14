"""add webhook_delivery_log outbox sequence unique constraint

Revision ID: 7e118c1893f0
Revises: a164b85bab9e
Create Date: 2026-07-14 13:40:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7e118c1893f0"
down_revision: str | Sequence[str] | None = "823974a5553e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "uq_webhook_delivery_log_sequence"


def upgrade() -> None:
    """Make ``webhook_delivery_log`` a durable outbox (#1606).

    ``DeliveryRepository.reserve_next_sequence()`` now allocates the next
    sequence number for a (tenant, media_buy, task_type) stream under a
    Postgres advisory transaction lock and inserts a ``"reserved"`` row
    BEFORE the HTTP send; ``mark_delivery_result()`` updates that same row
    afterward. The unique constraint added here is the DB-level backstop:
    a concurrent allocator (a second scheduler tick, an overlapping manual
    trigger) that somehow bypassed the advisory lock hits a unique
    violation instead of silently double-booking a sequence number.

    Pre-existing rows predate the reservation step: the sequence number was
    computed from ``MAX(sequence_number)`` over ``status = 'success'`` rows
    only (see the old ``get_max_sequence_number``), so a permanently-failed
    send at sequence N followed by a later successful send that also landed
    on N is a real, already-persisted duplicate under the new
    (tenant_id, media_buy_id, task_type, sequence_number) key. Dedupe those
    before adding the constraint so the migration cannot fail on existing
    data: for each conflicting group, keep the "success" row if one exists
    (ties broken by highest id), otherwise keep the highest id.
    """
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY tenant_id, media_buy_id, task_type, sequence_number
                    ORDER BY (status = 'success') DESC, id DESC
                ) AS rn
            FROM webhook_delivery_log
        )
        DELETE FROM webhook_delivery_log
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )
    op.create_unique_constraint(
        _CONSTRAINT_NAME,
        "webhook_delivery_log",
        ["tenant_id", "media_buy_id", "task_type", "sequence_number"],
    )


def downgrade() -> None:
    """Drop the outbox unique constraint.

    The upgrade's dedup DELETEs are not reversible (the duplicate rows are
    gone), but that data loss is intentional cleanup of pre-existing
    integrity violations, not something the downgrade needs to restore.
    """
    op.drop_constraint(_CONSTRAINT_NAME, "webhook_delivery_log", type_="unique")

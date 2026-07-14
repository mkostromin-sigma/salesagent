"""Delivery repository — tenant-scoped data access for webhook delivery tables.

Covers two ORM models:
- WebhookDeliveryRecord: webhook payload snapshots with retry tracking
- WebhookDeliveryLog: delivery report webhook sends with sequence tracking,
  doubling as a durable outbox (#1606) via reserve_next_sequence() / mark_delivery_result()

Core invariant: every query includes tenant_id in the WHERE clause. The tenant_id
is set at construction time and injected into all queries automatically.

Write methods add objects to the session but never commit — the caller (or UoW)
handles commit/rollback at the boundary. The one exception is documented on
``reserve_next_sequence``: the caller MUST commit immediately after calling it
to release the Postgres advisory transaction lock taken internally.

beads: salesagent-7x3i
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from src.core.database.models import WebhookDeliveryLog, WebhookDeliveryRecord


class DeliveryRepository:
    """Tenant-scoped data access for WebhookDeliveryRecord and WebhookDeliveryLog.

    All queries filter by tenant_id automatically. Write methods add objects
    to the session but never commit — the Unit of Work handles that.

    Args:
        session: SQLAlchemy session (caller manages lifecycle).
        tenant_id: Tenant scope for all queries.
    """

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    # ------------------------------------------------------------------
    # WebhookDeliveryRecord reads
    # ------------------------------------------------------------------

    def get_record_by_id(self, delivery_id: str) -> WebhookDeliveryRecord | None:
        """Get a delivery record by its ID within the tenant."""
        return self._session.scalars(
            select(WebhookDeliveryRecord).where(
                WebhookDeliveryRecord.tenant_id == self._tenant_id,
                WebhookDeliveryRecord.delivery_id == delivery_id,
            )
        ).first()

    def list_records_by_tenant(
        self,
        *,
        status: str | None = None,
        event_type: str | None = None,
        limit: int | None = None,
    ) -> list[WebhookDeliveryRecord]:
        """List delivery records for the tenant, with optional filters.

        Args:
            status: Filter by delivery status (e.g., "pending", "delivered", "failed").
            event_type: Filter by event type (e.g., "creative.status_changed").
            limit: Maximum number of records to return.
        """
        stmt = select(WebhookDeliveryRecord).where(
            WebhookDeliveryRecord.tenant_id == self._tenant_id,
        )
        if status is not None:
            stmt = stmt.where(WebhookDeliveryRecord.status == status)
        if event_type is not None:
            stmt = stmt.where(WebhookDeliveryRecord.event_type == event_type)
        stmt = stmt.order_by(WebhookDeliveryRecord.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.scalars(stmt).all())

    # ------------------------------------------------------------------
    # WebhookDeliveryRecord writes
    # ------------------------------------------------------------------

    def create_record(
        self,
        *,
        delivery_id: str,
        webhook_url: str,
        payload: dict[str, Any],
        event_type: str,
        object_id: str | None = None,
        status: str = "pending",
        attempts: int = 0,
        created_at: datetime | None = None,
    ) -> WebhookDeliveryRecord:
        """Create a new webhook delivery record.

        Does NOT commit — the caller handles that.
        """
        record = WebhookDeliveryRecord(
            delivery_id=delivery_id,
            tenant_id=self._tenant_id,
            webhook_url=webhook_url,
            payload=payload,
            event_type=event_type,
            object_id=object_id,
            status=status,
            attempts=attempts,
        )
        if created_at is not None:
            record.created_at = created_at
        self._session.add(record)
        self._session.flush()
        return record

    def update_record(
        self,
        delivery_id: str,
        *,
        status: str | None = None,
        attempts: int | None = None,
        response_code: int | None = None,
        last_error: str | None = None,
        last_attempt_at: datetime | None = None,
        delivered_at: datetime | None = None,
    ) -> WebhookDeliveryRecord | None:
        """Update fields on a delivery record within this tenant.

        Returns the updated record, or None if not found.
        Does NOT commit — the caller handles that.
        """
        record = self.get_record_by_id(delivery_id)
        if record is None:
            return None
        if status is not None:
            record.status = status
        if attempts is not None:
            record.attempts = attempts
        if response_code is not None:
            record.response_code = response_code
        if last_error is not None:
            record.last_error = last_error
        if last_attempt_at is not None:
            record.last_attempt_at = last_attempt_at
        if delivered_at is not None:
            record.delivered_at = delivered_at
        self._session.flush()
        return record

    # ------------------------------------------------------------------
    # WebhookDeliveryLog reads
    # ------------------------------------------------------------------

    def get_logs_by_webhook_id(
        self,
        media_buy_id: str,
        *,
        task_type: str | None = None,
        status: str | None = None,
    ) -> list[WebhookDeliveryLog]:
        """Get delivery logs for a media buy within the tenant.

        Args:
            media_buy_id: The media buy to get logs for.
            task_type: Filter by task type (e.g., "media_buy_delivery").
            status: Filter by log status (e.g., "success", "failed").
        """
        stmt = select(WebhookDeliveryLog).where(
            WebhookDeliveryLog.tenant_id == self._tenant_id,
            WebhookDeliveryLog.media_buy_id == media_buy_id,
        )
        if task_type is not None:
            stmt = stmt.where(WebhookDeliveryLog.task_type == task_type)
        if status is not None:
            stmt = stmt.where(WebhookDeliveryLog.status == status)
        stmt = stmt.order_by(WebhookDeliveryLog.created_at.desc())
        return list(self._session.scalars(stmt).all())

    def get_recent_successful_log(
        self,
        media_buy_id: str,
        *,
        task_type: str,
        since: datetime,
        notification_type: str | None = None,
    ) -> WebhookDeliveryLog | None:
        """Find a recent successful log entry (for duplicate detection).

        Used by the scheduler to check if a report was already sent within a
        rolling window. When ``notification_type`` is None the check spans ANY
        notification_type (the broadened #1570 dedup — a sent "final" must also
        suppress a re-send); pass a value to scope the check to one type.
        """
        stmt = select(WebhookDeliveryLog).where(
            WebhookDeliveryLog.tenant_id == self._tenant_id,
            WebhookDeliveryLog.media_buy_id == media_buy_id,
            WebhookDeliveryLog.task_type == task_type,
            WebhookDeliveryLog.status == "success",
            WebhookDeliveryLog.created_at > since,
        )
        if notification_type is not None:
            stmt = stmt.where(WebhookDeliveryLog.notification_type == notification_type)
        return self._session.scalars(stmt).first()

    def _max_allocated_sequence(self, media_buy_id: str, *, task_type: str) -> int:
        """Max sequence number allocated to ANY row in this stream, any status.

        Counts every status, not just "success": a "reserved" or "retrying"
        row already HOLDS that sequence number (allocated by
        ``reserve_next_sequence`` before its HTTP attempt even ran), so the
        next allocation must skip past it too, or two rows would race onto
        the same number and hit ``uq_webhook_delivery_log_sequence``.
        """
        result = self._session.scalar(
            select(func.coalesce(func.max(WebhookDeliveryLog.sequence_number), 0)).where(
                WebhookDeliveryLog.tenant_id == self._tenant_id,
                WebhookDeliveryLog.media_buy_id == media_buy_id,
                WebhookDeliveryLog.task_type == task_type,
            )
        )
        return result or 0

    def get_max_sequence_number(
        self,
        media_buy_id: str,
        *,
        task_type: str,
    ) -> int:
        """Get the maximum allocated sequence number for a media buy's stream.

        Counts rows in ANY status (see ``_max_allocated_sequence``) — a
        "reserved" or "retrying" row already holds that number even before a
        successful delivery. Returns 0 if no logs exist (caller should add 1
        for the next sequence, or use ``reserve_next_sequence`` which does
        this allocation under an advisory lock).
        """
        return self._max_allocated_sequence(media_buy_id, task_type=task_type)

    # ------------------------------------------------------------------
    # WebhookDeliveryLog writes
    # ------------------------------------------------------------------

    def create_log(
        self,
        *,
        log_id: str,
        principal_id: str,
        media_buy_id: str,
        webhook_url: str,
        task_type: str,
        status: str,
        attempt_count: int = 1,
        sequence_number: int = 1,
        notification_type: str | None = None,
        http_status_code: int | None = None,
        error_message: str | None = None,
        payload_size_bytes: int | None = None,
        response_time_ms: int | None = None,
        completed_at: datetime | None = None,
        next_retry_at: datetime | None = None,
    ) -> WebhookDeliveryLog:
        """Create or update a webhook delivery log entry.

        Uses session.merge() to handle upsert semantics (the protocol webhook
        service updates the same log entry across retry attempts).

        Does NOT commit — the caller handles that.
        """
        log_entry = WebhookDeliveryLog(
            id=log_id,
            tenant_id=self._tenant_id,
            principal_id=principal_id,
            media_buy_id=media_buy_id,
            webhook_url=webhook_url,
            task_type=task_type,
            status=status,
            attempt_count=attempt_count,
            sequence_number=sequence_number,
            notification_type=notification_type,
            http_status_code=http_status_code,
            error_message=error_message,
            payload_size_bytes=payload_size_bytes,
            response_time_ms=response_time_ms,
            completed_at=completed_at,
            next_retry_at=next_retry_at,
        )
        self._session.merge(log_entry)
        self._session.flush()
        return log_entry

    # ------------------------------------------------------------------
    # WebhookDeliveryLog durable outbox (#1606)
    # ------------------------------------------------------------------

    def get_inflight_reservation(
        self,
        media_buy_id: str,
        *,
        task_type: str,
        notification_type: str | None = None,
    ) -> WebhookDeliveryLog | None:
        """Return the oldest unresolved outbox row for this stream, if any.

        Used before allocating a new sequence: a prior attempt that reserved
        then failed its durable ack (or is still ``retrying``) must be
        recovered against the SAME reserved sequence — never mint a new one
        (#1606 recovery invariant).
        """
        stmt = select(WebhookDeliveryLog).where(
            WebhookDeliveryLog.tenant_id == self._tenant_id,
            WebhookDeliveryLog.media_buy_id == media_buy_id,
            WebhookDeliveryLog.task_type == task_type,
            WebhookDeliveryLog.status.in_(("reserved", "retrying")),
        )
        if notification_type is not None:
            stmt = stmt.where(WebhookDeliveryLog.notification_type == notification_type)
        stmt = stmt.order_by(WebhookDeliveryLog.created_at.asc())
        return self._session.scalars(stmt).first()

    def reserve_next_sequence(
        self,
        *,
        media_buy_id: str,
        task_type: str,
        log_id: str,
        principal_id: str,
        webhook_url: str,
        notification_type: str | None = None,
    ) -> WebhookDeliveryLog:
        """Durably reserve the next sequence number for a delivery-webhook stream.

        Takes a Postgres advisory transaction lock scoped to
        ``(tenant_id, media_buy_id, task_type)`` so concurrent reservations
        for the SAME stream (an overlapping scheduler tick, a manual trigger
        racing the hourly batch) serialize instead of both computing the same
        "next" number. Under the lock, allocates
        ``MAX(sequence_number over ALL statuses) + 1`` and inserts a
        ``status="reserved"`` row holding it, then flushes.

        ``notification_type`` is stamped at reservation time so one-shot
        ``has_final_notification`` can see an in-flight final before HTTP
        completes (otherwise a second enqueue races a blank reserved row).

        The lock is released on COMMIT or ROLLBACK of the current
        transaction — the CALLER MUST commit this session immediately after
        calling this method (before making the HTTP call) so the
        reservation is durable and the lock is released promptly. Once
        reserved, the sequence number is never re-derived: the deliverer
        calls ``mark_delivery_result`` on this same row after the HTTP
        attempt, whether it succeeds or fails.
        """
        lock_key = f"{self._tenant_id}:{media_buy_id}:{task_type}"
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"), {"lock_key": lock_key}
        )
        # Prefer reclaiming an unresolved reservation for this notification
        # intent under the same lock, so recovery never mints a duplicate seq.
        existing = self.get_inflight_reservation(media_buy_id, task_type=task_type, notification_type=notification_type)
        if existing is not None:
            if webhook_url:
                existing.webhook_url = webhook_url
            self._session.flush()
            return existing
        next_sequence = self._max_allocated_sequence(media_buy_id, task_type=task_type) + 1
        log_entry = WebhookDeliveryLog(
            id=log_id,
            tenant_id=self._tenant_id,
            principal_id=principal_id,
            media_buy_id=media_buy_id,
            webhook_url=webhook_url,
            task_type=task_type,
            status="reserved",
            attempt_count=0,
            sequence_number=next_sequence,
            notification_type=notification_type,
        )
        self._session.add(log_entry)
        self._session.flush()
        return log_entry

    def mark_delivery_result(
        self,
        log_id: str,
        *,
        status: str,
        attempt_count: int,
        notification_type: str | None = None,
        http_status_code: int | None = None,
        error_message: str | None = None,
        payload_size_bytes: int | None = None,
        response_time_ms: int | None = None,
        completed_at: datetime | None = None,
        next_retry_at: datetime | None = None,
    ) -> WebhookDeliveryLog:
        """Update a reserved/retrying outbox row with the outcome of an HTTP attempt.

        Unlike ``create_log`` (upsert via merge — used by callers that never
        reserve), this method RAISES ``ValueError`` if the row does not exist
        in this tenant instead of silently creating one: the row was created
        by ``reserve_next_sequence`` before the HTTP call, so a missing row
        means the caller has a bug (wrong log_id, wrong tenant) that must
        surface loudly rather than fabricate a fresh log entry with a
        possibly-conflicting sequence number.

        Does NOT commit — the caller commits (and must not swallow a raised
        exception on the success path: a durable-ack write failure means the
        delivery must be reported as failed even though the HTTP call may
        have succeeded).
        """
        log_entry = self._session.get(WebhookDeliveryLog, log_id)
        if log_entry is None or log_entry.tenant_id != self._tenant_id:
            raise ValueError(f"WebhookDeliveryLog {log_id!r} not found for tenant {self._tenant_id!r}")
        log_entry.status = status
        log_entry.attempt_count = attempt_count
        if notification_type is not None:
            log_entry.notification_type = notification_type
        if http_status_code is not None:
            log_entry.http_status_code = http_status_code
        if error_message is not None:
            log_entry.error_message = error_message
        if payload_size_bytes is not None:
            log_entry.payload_size_bytes = payload_size_bytes
        if response_time_ms is not None:
            log_entry.response_time_ms = response_time_ms
        if completed_at is not None:
            log_entry.completed_at = completed_at
        if next_retry_at is not None:
            log_entry.next_retry_at = next_retry_at
        self._session.flush()
        return log_entry

    def has_final_notification(self, media_buy_id: str, *, task_type: str) -> bool:
        """True if a "final" notification already exists for this stream, delivered or in-flight.

        Blocks a second final from ever being enqueued: "final" is one-shot
        per AdCP's "one final notification when the campaign completes"
        commitment (optimization-reporting.mdx Publisher Commitment), so
        this returns True for a delivered ("success") final AND for one
        still in flight ("reserved" / "retrying") — the caller must not
        race a second final attempt against an unresolved first one.
        """
        stmt = select(WebhookDeliveryLog.id).where(
            WebhookDeliveryLog.tenant_id == self._tenant_id,
            WebhookDeliveryLog.media_buy_id == media_buy_id,
            WebhookDeliveryLog.task_type == task_type,
            WebhookDeliveryLog.notification_type == "final",
            WebhookDeliveryLog.status.in_(("success", "reserved", "retrying")),
        )
        return self._session.scalars(stmt).first() is not None

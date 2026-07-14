"""Integration tests for DeliveryRepository.

Tests the repository pattern with real PostgreSQL to verify:
- Tenant isolation (core invariant: every query is tenant-scoped by construction)
- CRUD operations for WebhookDeliveryRecord and WebhookDeliveryLog
- Filtering and ordering behavior
- Sequence number tracking

beads: salesagent-7x3i
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from src.core.database.database_session import get_db_session
from src.core.database.models import (
    MediaBuy,
    Principal,
    Tenant,
    WebhookDeliveryLog,
    WebhookDeliveryRecord,
)
from src.core.database.repositories.delivery import DeliveryRepository

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cleanup_tenant(tenant_id: str) -> None:
    """Delete tenant and all dependent data (correct FK order)."""
    with get_db_session() as session:
        session.execute(delete(WebhookDeliveryLog).where(WebhookDeliveryLog.tenant_id == tenant_id))
        session.execute(delete(WebhookDeliveryRecord).where(WebhookDeliveryRecord.tenant_id == tenant_id))
        session.execute(delete(MediaBuy).where(MediaBuy.tenant_id == tenant_id))
        session.execute(delete(Principal).where(Principal.tenant_id == tenant_id))
        session.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
        session.commit()


@pytest.fixture
def tenant_a(integration_db):
    """Create tenant A for delivery repository tests."""
    tenant_id = "del_repo_tenant_a"
    with get_db_session() as session:
        tenant = Tenant(
            tenant_id=tenant_id, name="Delivery Tenant A", subdomain="del-a", is_active=True, ad_server="mock"
        )
        session.add(tenant)
        session.commit()
    yield tenant_id
    _cleanup_tenant(tenant_id)


@pytest.fixture
def tenant_b(integration_db):
    """Create tenant B (for cross-tenant isolation tests)."""
    tenant_id = "del_repo_tenant_b"
    with get_db_session() as session:
        tenant = Tenant(
            tenant_id=tenant_id, name="Delivery Tenant B", subdomain="del-b", is_active=True, ad_server="mock"
        )
        session.add(tenant)
        session.commit()
    yield tenant_id
    _cleanup_tenant(tenant_id)


@pytest.fixture
def principal_a(tenant_a):
    """Create a principal in tenant A."""
    principal_id = "del_principal_a"
    with get_db_session() as session:
        principal = Principal(
            tenant_id=tenant_a,
            principal_id=principal_id,
            name="Delivery Advertiser A",
            access_token="del_token_a",
            platform_mappings={"mock": {"advertiser_id": "del_adv_a"}},
        )
        session.add(principal)
        session.commit()
    yield principal_id


@pytest.fixture
def principal_b(tenant_b):
    """Create a principal in tenant B."""
    principal_id = "del_principal_b"
    with get_db_session() as session:
        principal = Principal(
            tenant_id=tenant_b,
            principal_id=principal_id,
            name="Delivery Advertiser B",
            access_token="del_token_b",
            platform_mappings={"mock": {"advertiser_id": "del_adv_b"}},
        )
        session.add(principal)
        session.commit()
    yield principal_id


@pytest.fixture
def media_buy_a(tenant_a, principal_a):
    """Create a media buy in tenant A for log tests."""
    media_buy_id = "del_mb_a"
    with get_db_session() as session:
        mb = MediaBuy(
            media_buy_id=media_buy_id,
            tenant_id=tenant_a,
            principal_id=principal_a,
            order_name="Delivery Order A",
            advertiser_name="Test Advertiser",
            start_date=datetime(2026, 1, 1).date(),
            end_date=datetime(2026, 12, 31).date(),
            status="active",
            raw_request={"test": True},
        )
        session.add(mb)
        session.commit()
    yield media_buy_id


@pytest.fixture
def media_buy_b(tenant_b, principal_b):
    """Create a media buy in tenant B for isolation tests."""
    media_buy_id = "del_mb_b"
    with get_db_session() as session:
        mb = MediaBuy(
            media_buy_id=media_buy_id,
            tenant_id=tenant_b,
            principal_id=principal_b,
            order_name="Delivery Order B",
            advertiser_name="Test Advertiser B",
            start_date=datetime(2026, 1, 1).date(),
            end_date=datetime(2026, 12, 31).date(),
            status="active",
            raw_request={"test": True},
        )
        session.add(mb)
        session.commit()
    yield media_buy_id


# ---------------------------------------------------------------------------
# WebhookDeliveryRecord tests
# ---------------------------------------------------------------------------


class TestCreateRecord:
    """create_record persists a delivery record with correct tenant isolation."""

    def test_creates_record_with_required_fields(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            record = repo.create_record(
                delivery_id="whd_test_001",
                webhook_url="https://example.com/hook",
                payload={"event": "test"},
                event_type="creative.status_changed",
            )
            session.commit()

        with get_db_session() as session:
            persisted = session.scalars(
                select(WebhookDeliveryRecord).where(WebhookDeliveryRecord.delivery_id == "whd_test_001")
            ).first()
            assert persisted is not None
            assert persisted.tenant_id == tenant_a
            assert persisted.webhook_url == "https://example.com/hook"
            assert persisted.payload == {"event": "test"}
            assert persisted.event_type == "creative.status_changed"
            assert persisted.status == "pending"
            assert persisted.attempts == 0

    def test_creates_record_with_optional_fields(self, tenant_a):
        now = datetime.now(UTC)
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            record = repo.create_record(
                delivery_id="whd_test_002",
                webhook_url="https://example.com/hook",
                payload={"event": "test"},
                event_type="media_buy.created",
                object_id="mb_123",
                status="delivered",
                attempts=2,
                created_at=now,
            )
            session.commit()

        with get_db_session() as session:
            persisted = session.scalars(
                select(WebhookDeliveryRecord).where(WebhookDeliveryRecord.delivery_id == "whd_test_002")
            ).first()
            assert persisted is not None
            assert persisted.object_id == "mb_123"
            assert persisted.status == "delivered"
            assert persisted.attempts == 2


class TestGetRecordById:
    """get_record_by_id returns only records belonging to the repository's tenant."""

    def test_returns_own_tenant_record(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_record(
                delivery_id="whd_own",
                webhook_url="https://example.com",
                payload={},
                event_type="test",
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.get_record_by_id("whd_own")
            assert result is not None
            assert result.delivery_id == "whd_own"

    def test_does_not_return_other_tenant_record(self, tenant_a, tenant_b):
        # Create record in tenant A
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_record(
                delivery_id="whd_cross",
                webhook_url="https://example.com",
                payload={},
                event_type="test",
            )
            session.commit()

        # Try to access from tenant B
        with get_db_session() as session:
            repo_b = DeliveryRepository(session, tenant_b)
            result = repo_b.get_record_by_id("whd_cross")
            assert result is None

    def test_returns_none_for_nonexistent(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.get_record_by_id("whd_nonexistent")
            assert result is None


class TestListRecordsByTenant:
    """list_records_by_tenant returns records with correct filtering."""

    def test_lists_all_tenant_records(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_record(delivery_id="whd_list_1", webhook_url="https://a.com", payload={}, event_type="test")
            repo.create_record(delivery_id="whd_list_2", webhook_url="https://b.com", payload={}, event_type="test")
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            results = repo.list_records_by_tenant()
            delivery_ids = {r.delivery_id for r in results}
            assert "whd_list_1" in delivery_ids
            assert "whd_list_2" in delivery_ids

    def test_filters_by_status(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_record(
                delivery_id="whd_st_1", webhook_url="https://a.com", payload={}, event_type="test", status="pending"
            )
            repo.create_record(
                delivery_id="whd_st_2", webhook_url="https://b.com", payload={}, event_type="test", status="delivered"
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            results = repo.list_records_by_tenant(status="delivered")
            assert len(results) >= 1
            assert all(r.status == "delivered" for r in results)

    def test_filters_by_event_type(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_record(
                delivery_id="whd_ev_1", webhook_url="https://a.com", payload={}, event_type="creative.status_changed"
            )
            repo.create_record(
                delivery_id="whd_ev_2", webhook_url="https://b.com", payload={}, event_type="media_buy.created"
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            results = repo.list_records_by_tenant(event_type="media_buy.created")
            assert len(results) >= 1
            assert all(r.event_type == "media_buy.created" for r in results)

    def test_respects_limit(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            for i in range(5):
                repo.create_record(
                    delivery_id=f"whd_lim_{i}", webhook_url="https://a.com", payload={}, event_type="test"
                )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            results = repo.list_records_by_tenant(limit=2)
            assert len(results) == 2

    def test_excludes_other_tenant(self, tenant_a, tenant_b):
        with get_db_session() as session:
            repo_a = DeliveryRepository(session, tenant_a)
            repo_a.create_record(delivery_id="whd_iso_a", webhook_url="https://a.com", payload={}, event_type="test")
            repo_b = DeliveryRepository(session, tenant_b)
            repo_b.create_record(delivery_id="whd_iso_b", webhook_url="https://b.com", payload={}, event_type="test")
            session.commit()

        with get_db_session() as session:
            repo_a = DeliveryRepository(session, tenant_a)
            results = repo_a.list_records_by_tenant()
            delivery_ids = {r.delivery_id for r in results}
            assert "whd_iso_a" in delivery_ids
            assert "whd_iso_b" not in delivery_ids


class TestUpdateRecord:
    """update_record modifies fields on existing records."""

    def test_updates_status_and_attempts(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_record(delivery_id="whd_upd_1", webhook_url="https://a.com", payload={}, event_type="test")
            session.commit()

        now = datetime.now(UTC)
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.update_record(
                "whd_upd_1",
                status="delivered",
                attempts=3,
                response_code=200,
                delivered_at=now,
            )
            session.commit()
            assert result is not None
            assert result.status == "delivered"
            assert result.attempts == 3
            assert result.response_code == 200

    def test_updates_error_fields(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_record(delivery_id="whd_upd_2", webhook_url="https://a.com", payload={}, event_type="test")
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.update_record(
                "whd_upd_2",
                status="failed",
                last_error="Connection timeout",
                last_attempt_at=datetime.now(UTC),
            )
            session.commit()
            assert result is not None
            assert result.status == "failed"
            assert result.last_error == "Connection timeout"

    def test_returns_none_for_nonexistent(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.update_record("whd_nonexistent", status="delivered")
            assert result is None

    def test_cannot_update_other_tenant_record(self, tenant_a, tenant_b):
        with get_db_session() as session:
            repo_a = DeliveryRepository(session, tenant_a)
            repo_a.create_record(
                delivery_id="whd_upd_cross", webhook_url="https://a.com", payload={}, event_type="test"
            )
            session.commit()

        with get_db_session() as session:
            repo_b = DeliveryRepository(session, tenant_b)
            result = repo_b.update_record("whd_upd_cross", status="delivered")
            assert result is None


# ---------------------------------------------------------------------------
# WebhookDeliveryLog tests
# ---------------------------------------------------------------------------


class TestCreateLog:
    """create_log persists delivery log entries with upsert semantics."""

    def test_creates_log_with_required_fields(self, tenant_a, principal_a, media_buy_a):
        log_id = str(uuid4())
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=log_id,
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com/delivery",
                task_type="media_buy_delivery",
                status="success",
            )
            session.commit()

        with get_db_session() as session:
            persisted = session.scalars(select(WebhookDeliveryLog).where(WebhookDeliveryLog.id == log_id)).first()
            assert persisted is not None
            assert persisted.tenant_id == tenant_a
            assert persisted.principal_id == principal_a
            assert persisted.media_buy_id == media_buy_a
            assert persisted.task_type == "media_buy_delivery"
            assert persisted.status == "success"

    def test_creates_log_with_all_optional_fields(self, tenant_a, principal_a, media_buy_a):
        log_id = str(uuid4())
        now = datetime.now(UTC)
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=log_id,
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com/delivery",
                task_type="media_buy_delivery",
                status="success",
                attempt_count=2,
                sequence_number=5,
                notification_type="scheduled",
                http_status_code=200,
                payload_size_bytes=1024,
                response_time_ms=150,
                completed_at=now,
            )
            session.commit()

        with get_db_session() as session:
            persisted = session.scalars(select(WebhookDeliveryLog).where(WebhookDeliveryLog.id == log_id)).first()
            assert persisted is not None
            assert persisted.attempt_count == 2
            assert persisted.sequence_number == 5
            assert persisted.notification_type == "scheduled"
            assert persisted.http_status_code == 200
            assert persisted.payload_size_bytes == 1024
            assert persisted.response_time_ms == 150

    def test_upsert_updates_existing_log(self, tenant_a, principal_a, media_buy_a):
        """create_log uses merge() so calling twice with same ID updates the record."""
        log_id = str(uuid4())
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=log_id,
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="retrying",
                attempt_count=1,
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=log_id,
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
                attempt_count=2,
                http_status_code=200,
            )
            session.commit()

        with get_db_session() as session:
            logs = session.scalars(select(WebhookDeliveryLog).where(WebhookDeliveryLog.id == log_id)).all()
            assert len(list(logs)) == 1
            log = session.scalars(select(WebhookDeliveryLog).where(WebhookDeliveryLog.id == log_id)).first()
            assert log.status == "success"
            assert log.attempt_count == 2
            assert log.http_status_code == 200


class TestGetLogsByWebhookId:
    """get_logs_by_webhook_id returns logs for a media buy within the tenant."""

    def test_returns_logs_for_media_buy(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            for _i in range(3):
                repo.create_log(
                    log_id=str(uuid4()),
                    principal_id=principal_a,
                    media_buy_id=media_buy_a,
                    webhook_url="https://example.com",
                    task_type="media_buy_delivery",
                    status="success",
                )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            logs = repo.get_logs_by_webhook_id(media_buy_a)
            assert len(logs) == 3

    def test_filters_by_task_type(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
            )
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="delivery_report",
                status="success",
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            logs = repo.get_logs_by_webhook_id(media_buy_a, task_type="media_buy_delivery")
            assert len(logs) == 1
            assert logs[0].task_type == "media_buy_delivery"

    def test_tenant_isolation(self, tenant_a, tenant_b, principal_a, principal_b, media_buy_a, media_buy_b):
        with get_db_session() as session:
            repo_a = DeliveryRepository(session, tenant_a)
            repo_a.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
            )
            repo_b = DeliveryRepository(session, tenant_b)
            repo_b.create_log(
                log_id=str(uuid4()),
                principal_id=principal_b,
                media_buy_id=media_buy_b,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
            )
            session.commit()

        with get_db_session() as session:
            repo_a = DeliveryRepository(session, tenant_a)
            logs_a = repo_a.get_logs_by_webhook_id(media_buy_a)
            assert len(logs_a) == 1
            assert logs_a[0].tenant_id == tenant_a


class TestGetRecentSuccessfulLog:
    """get_recent_successful_log finds logs for duplicate detection."""

    def test_finds_recent_successful_log(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
                notification_type="scheduled",
            )
            session.commit()

        one_day_ago = datetime.now(UTC) - timedelta(hours=24)
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.get_recent_successful_log(
                media_buy_a,
                task_type="media_buy_delivery",
                notification_type="scheduled",
                since=one_day_ago,
            )
            assert result is not None
            assert result.status == "success"

    def test_returns_none_when_no_recent_log(self, tenant_a, principal_a, media_buy_a):
        # No logs created
        future = datetime.now(UTC) + timedelta(hours=1)
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.get_recent_successful_log(
                media_buy_a,
                task_type="media_buy_delivery",
                notification_type="scheduled",
                since=future,
            )
            assert result is None

    def test_ignores_failed_logs(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="failed",
                notification_type="scheduled",
            )
            session.commit()

        one_day_ago = datetime.now(UTC) - timedelta(hours=24)
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.get_recent_successful_log(
                media_buy_a,
                task_type="media_buy_delivery",
                notification_type="scheduled",
                since=one_day_ago,
            )
            assert result is None


class TestGetMaxSequenceNumber:
    """get_max_sequence_number tracks sequence progression."""

    def test_returns_zero_when_no_logs(self, tenant_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.get_max_sequence_number(media_buy_a, task_type="media_buy_delivery")
            assert result == 0

    def test_returns_max_sequence(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            for seq in [1, 2, 3]:
                repo.create_log(
                    log_id=str(uuid4()),
                    principal_id=principal_a,
                    media_buy_id=media_buy_a,
                    webhook_url="https://example.com",
                    task_type="media_buy_delivery",
                    status="success",
                    sequence_number=seq,
                )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            result = repo.get_max_sequence_number(media_buy_a, task_type="media_buy_delivery")
            assert result == 3

    def test_scoped_to_task_type(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
                sequence_number=5,
            )
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="delivery_report",
                status="success",
                sequence_number=10,
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            assert repo.get_max_sequence_number(media_buy_a, task_type="media_buy_delivery") == 5
            assert repo.get_max_sequence_number(media_buy_a, task_type="delivery_report") == 10

    def test_counts_reserved_and_retrying_rows_not_just_success(self, tenant_a, principal_a, media_buy_a):
        """#1606 point 5: a "reserved" or "retrying" row already holds its number.

        The old behavior (success-only) would return 1 here even though
        sequence 2 is already allocated to a "reserved" row — a caller
        deriving "next" from the old semantics would collide with it.
        """
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
                sequence_number=1,
            )
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="reserved",
                sequence_number=2,
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            assert repo.get_max_sequence_number(media_buy_a, task_type="media_buy_delivery") == 2


class TestReserveNextSequence:
    """reserve_next_sequence durably allocates the next outbox sequence number.

    Covers: #1606 point 3 (L1)
    """

    def test_first_reservation_allocates_sequence_one(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            log_entry = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
            )
            session.commit()
            assert log_entry.sequence_number == 1
            assert log_entry.status == "reserved"
            assert log_entry.attempt_count == 0

    def test_reservation_skips_past_prior_allocations_of_any_status(self, tenant_a, principal_a, media_buy_a):
        """The next reservation is MAX(all statuses) + 1, not MAX(success) + 1."""
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="failed",
                sequence_number=1,
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            log_entry = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
            )
            session.commit()
            assert log_entry.sequence_number == 2

    def test_reservation_persists_durably_across_sessions(self, tenant_a, principal_a, media_buy_a):
        log_id = str(uuid4())
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=log_id,
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
            )
            session.commit()

        with get_db_session() as session:
            persisted = session.scalars(select(WebhookDeliveryLog).where(WebhookDeliveryLog.id == log_id)).first()
            assert persisted is not None
            assert persisted.status == "reserved"
            assert persisted.sequence_number == 1

    def test_second_reservation_scoped_to_different_media_buy_starts_at_one(self, tenant_a, principal_a, media_buy_a):
        """Reservation streams are scoped per (tenant, media_buy, task_type) — a
        different media_buy's stream is unaffected by another's allocations."""
        other_media_buy_id = "del_mb_a_other"
        with get_db_session() as session:
            mb = MediaBuy(
                media_buy_id=other_media_buy_id,
                tenant_id=tenant_a,
                principal_id=principal_a,
                order_name="Other Order",
                advertiser_name="Test Advertiser",
                start_date=datetime(2026, 1, 1).date(),
                end_date=datetime(2026, 12, 31).date(),
                status="active",
                raw_request={"test": True},
            )
            session.add(mb)
            repo = DeliveryRepository(session, tenant_a)
            repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
                notification_type="scheduled",
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            log_entry = repo.reserve_next_sequence(
                media_buy_id=other_media_buy_id,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
                notification_type="scheduled",
            )
            session.commit()
            assert log_entry.sequence_number == 1

    def test_second_reserve_reclaims_inflight_same_notification_type(self, tenant_a, principal_a, media_buy_a):
        """Recovery must reuse the reserved sequence, not mint a new one."""
        first_id = str(uuid4())
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            first = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=first_id,
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
                notification_type="scheduled",
            )
            session.commit()
            assert first.sequence_number == 1

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            second = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook-retry",
                notification_type="scheduled",
            )
            session.commit()
            assert second.id == first_id
            assert second.sequence_number == 1
            assert second.webhook_url == "https://example.com/webhook-retry"

    def test_final_reserve_can_coexist_with_scheduled_inflight(self, tenant_a, principal_a, media_buy_a):
        """Different notification_type streams reclaim independently."""
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            scheduled = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
                notification_type="scheduled",
            )
            session.commit()
            assert scheduled.sequence_number == 1

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            final = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
                notification_type="final",
            )
            session.commit()
            assert final.sequence_number == 2
            assert final.notification_type == "final"
            assert final.id != scheduled.id


class TestUniqueSequenceConstraint:
    """uq_webhook_delivery_log_sequence backstops the reservation logic (#1606 point 2/L1)."""

    def test_duplicate_sequence_for_same_stream_violates_unique_constraint(self, tenant_a, principal_a, media_buy_a):
        """A second row allocated to a sequence number already held in this
        (tenant, media_buy, task_type) stream is rejected at the DB level —
        the backstop for a bug that bypasses reserve_next_sequence's advisory
        lock and computes a colliding sequence number directly."""
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
                sequence_number=1,
            )
            session.commit()

        with pytest.raises(IntegrityError):
            with get_db_session() as session:
                repo = DeliveryRepository(session, tenant_a)
                repo.create_log(
                    log_id=str(uuid4()),
                    principal_id=principal_a,
                    media_buy_id=media_buy_a,
                    webhook_url="https://example.com",
                    task_type="media_buy_delivery",
                    status="success",
                    sequence_number=1,
                )
                session.commit()

    def test_same_sequence_allowed_across_different_task_types(self, tenant_a, principal_a, media_buy_a):
        """The unique key includes task_type, so two independent streams for
        the SAME media buy may each legitimately hold sequence 1."""
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
                sequence_number=1,
            )
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="delivery_report",
                status="success",
                sequence_number=1,
            )
            session.commit()  # must not raise


class TestMarkDeliveryResult:
    """mark_delivery_result updates a reserved/retrying outbox row with an HTTP outcome.

    Covers: #1606 point 4 (L1)
    """

    def test_updates_reserved_row_to_success(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            log_entry = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
            )
            log_id = log_entry.id
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            updated = repo.mark_delivery_result(
                log_id,
                status="success",
                attempt_count=1,
                notification_type="scheduled",
                http_status_code=200,
                completed_at=datetime.now(UTC),
            )
            session.commit()
            assert updated.status == "success"
            assert updated.attempt_count == 1
            assert updated.notification_type == "scheduled"
            assert updated.http_status_code == 200
            # The sequence number allocated at reservation time is untouched.
            assert updated.sequence_number == 1

    def test_does_not_create_new_row_for_second_attempt(self, tenant_a, principal_a, media_buy_a):
        """Marking "retrying" then "success" updates the SAME row — no new
        sequence number is ever allocated after the initial reservation."""
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            log_entry = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
            )
            log_id = log_entry.id
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.mark_delivery_result(log_id, status="retrying", attempt_count=1, error_message="HTTP 503")
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.mark_delivery_result(log_id, status="success", attempt_count=2, http_status_code=200)
            session.commit()

        with get_db_session() as session:
            rows = session.scalars(
                select(WebhookDeliveryLog).where(WebhookDeliveryLog.media_buy_id == media_buy_a)
            ).all()
            assert len(list(rows)) == 1
            row = rows[0]
            assert row.status == "success"
            assert row.attempt_count == 2
            assert row.sequence_number == 1

    def test_raises_for_nonexistent_log_id(self, tenant_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            with pytest.raises(ValueError):
                repo.mark_delivery_result("does-not-exist", status="success", attempt_count=1)

    def test_raises_for_other_tenant_log_id(self, tenant_a, tenant_b, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            log_entry = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
            )
            log_id = log_entry.id
            session.commit()

        with get_db_session() as session:
            repo_b = DeliveryRepository(session, tenant_b)
            with pytest.raises(ValueError):
                repo_b.mark_delivery_result(log_id, status="success", attempt_count=1)


class TestHasFinalNotification:
    """has_final_notification is the one-shot guard for terminal delivery webhooks.

    Covers: #1606 points 8/12 (L1)
    """

    def test_false_when_no_logs_exist(self, tenant_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            assert repo.has_final_notification(media_buy_a, task_type="media_buy_delivery") is False

    def test_false_when_only_scheduled_logs_exist(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
                notification_type="scheduled",
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            assert repo.has_final_notification(media_buy_a, task_type="media_buy_delivery") is False

    def test_true_when_final_succeeded(self, tenant_a, principal_a, media_buy_a):
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="success",
                notification_type="final",
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            assert repo.has_final_notification(media_buy_a, task_type="media_buy_delivery") is True

    def test_true_when_final_reserved_but_not_yet_resolved(self, tenant_a, principal_a, media_buy_a):
        """An in-flight (not yet succeeded/failed) final also blocks a second one."""
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            log_entry = repo.reserve_next_sequence(
                media_buy_id=media_buy_a,
                task_type="media_buy_delivery",
                log_id=str(uuid4()),
                principal_id=principal_a,
                webhook_url="https://example.com/webhook",
            )
            repo.mark_delivery_result(log_entry.id, status="retrying", attempt_count=1, notification_type="final")
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            assert repo.has_final_notification(media_buy_a, task_type="media_buy_delivery") is True

    def test_false_when_final_failed_permanently(self, tenant_a, principal_a, media_buy_a):
        """A permanently-failed final does NOT block a retry attempt — only
        success/reserved/retrying count as "already delivered or in flight"."""
        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            repo.create_log(
                log_id=str(uuid4()),
                principal_id=principal_a,
                media_buy_id=media_buy_a,
                webhook_url="https://example.com",
                task_type="media_buy_delivery",
                status="failed",
                notification_type="final",
            )
            session.commit()

        with get_db_session() as session:
            repo = DeliveryRepository(session, tenant_a)
            assert repo.has_final_notification(media_buy_a, task_type="media_buy_delivery") is False

"""Integration tests for the durable delivery-webhook outbox (#1606).

Drives the REAL scheduler entrypoints (``DeliveryWebhookScheduler._send_report_for_media_buy``,
``enqueue_final_if_configured``, ``trigger_report_for_media_buy_by_id``) against a real
PostgreSQL database via the ``DeliveryPollEnv`` harness — only the outbound HTTP POST is
mocked. This is the L5 real-entrypoint coverage for the reserve -> HTTP -> mark_result
outbox lifecycle: reservation durability, one-shot final semantics, sequence monotonicity
across retries, and the "no swallow on durable-ack failure" contract.

Covers leaf issue #1641 (L5 — real-entrypoint tests) of the durable webhook
outbox work (#1606).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import requests
from sqlalchemy import select

from src.core.database.models import WebhookDeliveryLog
from src.core.database.repositories.delivery import DeliveryRepository
from src.services.delivery_webhook_scheduler import DeliveryWebhookScheduler
from tests.factories import MediaBuyFactory, PrincipalFactory, TenantFactory
from tests.harness import DeliveryPollEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

_WEBHOOK = {"url": "https://example.com/webhook", "reporting_frequency": "daily"}


def _ok_response() -> MagicMock:
    resp = MagicMock(status_code=200, text="OK")
    resp.raise_for_status.return_value = None
    return resp


def _make_active_buy(env: DeliveryPollEnv, media_buy_id: str = "mb_outbox_1", **overrides):
    tenant = TenantFactory(tenant_id="t1")
    principal = PrincipalFactory(tenant=tenant, principal_id="p1")
    kwargs = {
        "tenant": tenant,
        "principal": principal,
        "media_buy_id": media_buy_id,
        "status": "active",
        "start_date": datetime.now(UTC).date() - timedelta(days=30),
        "end_date": datetime.now(UTC).date() + timedelta(days=30),
        "raw_request": {"reporting_webhook": dict(_WEBHOOK)},
    }
    kwargs.update(overrides)
    buy = MediaBuyFactory(**kwargs)
    env.set_adapter_response(buy.media_buy_id, impressions=5000)
    return buy


class TestScheduledSendReservesAndMarksSuccess:
    """A successful scheduled send goes through reserve -> HTTP -> mark success.

    Covers: #1606 points 3/6 (L5)
    """

    @pytest.mark.asyncio
    async def test_reserved_row_is_updated_to_success_not_duplicated(self, integration_db):
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(env)
            scheduler = DeliveryWebhookScheduler()

            with patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()):
                delivered = await scheduler._send_report_for_media_buy(
                    buy, buy.raw_request["reporting_webhook"], env.get_session(), force=True
                )

            assert delivered is True

            rows = list(
                env.get_session().scalars(
                    select(WebhookDeliveryLog).where(WebhookDeliveryLog.media_buy_id == buy.media_buy_id)
                )
            )
            assert len(rows) == 1, "reserve + mark must update the SAME row, never insert a second"
            assert rows[0].status == "success"
            assert rows[0].sequence_number == 1
            assert rows[0].notification_type == "scheduled"

    @pytest.mark.asyncio
    async def test_second_send_allocates_sequence_two_not_one(self, integration_db):
        """A second scheduled send (simulating the next day) reserves sequence 2."""
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(env)
            scheduler = DeliveryWebhookScheduler()

            with patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()):
                await scheduler._send_report_for_media_buy(
                    buy, buy.raw_request["reporting_webhook"], env.get_session(), force=True
                )
                delivered_2 = await scheduler._send_report_for_media_buy(
                    buy, buy.raw_request["reporting_webhook"], env.get_session(), force=True
                )

            assert delivered_2 is True
            repo = DeliveryRepository(env.get_session(), "t1")
            assert repo.get_max_sequence_number(buy.media_buy_id, task_type="media_buy_delivery") == 2


class TestFailedHttpMarksReservedRowFailedNotSuccess:
    """A permanently-failing HTTP send updates the reserved row to "failed",
    never leaving it stuck at "reserved" and never marking it "success".

    Covers: #1606 points 3/6/7 (L5)
    """

    @pytest.mark.asyncio
    async def test_4xx_marks_row_failed(self, integration_db):
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(env)
            scheduler = DeliveryWebhookScheduler()

            error_response = MagicMock(status_code=403, text="Forbidden")
            error_response.raise_for_status.side_effect = requests.HTTPError(response=error_response)

            with (
                patch.object(scheduler.webhook_service._session, "post", return_value=error_response),
                pytest.raises(RuntimeError, match="webhook send failed"),
            ):
                await scheduler._send_report_for_media_buy(
                    buy, buy.raw_request["reporting_webhook"], env.get_session(), force=True
                )

            rows = list(
                env.get_session().scalars(
                    select(WebhookDeliveryLog).where(WebhookDeliveryLog.media_buy_id == buy.media_buy_id)
                )
            )
            assert len(rows) == 1
            assert rows[0].status == "failed"
            assert rows[0].sequence_number == 1


class TestEnqueueFinalIfConfigured:
    """enqueue_final_if_configured is the transition-triggered one-shot final send.

    Covers: #1606 point 9 (L5)
    """

    @pytest.mark.asyncio
    async def test_no_reporting_webhook_configured_is_a_noop(self, integration_db):
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(env, media_buy_id="mb_no_webhook", raw_request={}, status="completed")
            scheduler = DeliveryWebhookScheduler()

            sent = await scheduler.enqueue_final_if_configured(buy, env.get_session())

            assert sent is False
            rows = list(
                env.get_session().scalars(
                    select(WebhookDeliveryLog).where(WebhookDeliveryLog.media_buy_id == buy.media_buy_id)
                )
            )
            assert rows == []

    @pytest.mark.asyncio
    async def test_sends_final_for_completed_buy_bypassing_status_filter(self, integration_db):
        """The buy is persisted as "completed" — outside REPORTABLE_CANONICAL_STATUSES'
        MediaBuyStatus enum handling for terminal statuses generally — and the final
        send must still find it (status_filter=None -> fetch-by-ID semantics)."""
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(env, media_buy_id="mb_completed", status="completed")
            scheduler = DeliveryWebhookScheduler()

            with patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()):
                sent = await scheduler.enqueue_final_if_configured(buy, env.get_session())

            assert sent is True
            row = (
                env.get_session()
                .scalars(select(WebhookDeliveryLog).where(WebhookDeliveryLog.media_buy_id == buy.media_buy_id))
                .one()
            )
            assert row.status == "success"
            assert row.notification_type == "final"

    @pytest.mark.asyncio
    async def test_second_call_does_not_send_a_second_final(self, integration_db):
        """Calling enqueue_final_if_configured twice (e.g. two transition sites
        both firing) must send exactly one final — the second call is a no-op."""
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(env, media_buy_id="mb_completed_twice", status="completed")
            scheduler = DeliveryWebhookScheduler()

            with patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()) as mock_post:
                sent_1 = await scheduler.enqueue_final_if_configured(buy, env.get_session())
                sent_2 = await scheduler.enqueue_final_if_configured(buy, env.get_session())

            assert sent_1 is True
            assert sent_2 is False
            assert mock_post.call_count == 1, "the second enqueue must not make a second HTTP call"
            rows = list(
                env.get_session().scalars(
                    select(WebhookDeliveryLog).where(WebhookDeliveryLog.media_buy_id == buy.media_buy_id)
                )
            )
            assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_paused_buy_has_no_delivery_data_so_no_report_is_sent(self, integration_db):
        """#1606 point 10: paused follows scheduled semantics (never final —
        pinned at the unit level by ``derive_notification_type`` in
        ``tests/unit/test_delivery.py::TestNotificationTypeDerivation``). At
        this real-entrypoint level: a buy persisted as "paused" resolves
        outside REPORTABLE_CANONICAL_STATUSES — it is neither selected by the
        SERVING batch query nor eligible for a final, so no report (scheduled
        or final) is ever sent for it while paused."""
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(env, media_buy_id="mb_paused", status="paused")
            scheduler = DeliveryWebhookScheduler()

            with patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()) as mock_post:
                delivered = await scheduler._send_report_for_media_buy(
                    buy, buy.raw_request["reporting_webhook"], env.get_session(), force=True
                )

            assert delivered is False
            mock_post.assert_not_called()
            rows = list(
                env.get_session().scalars(
                    select(WebhookDeliveryLog).where(WebhookDeliveryLog.media_buy_id == buy.media_buy_id)
                )
            )
            assert rows == [], "no outbox row should be reserved for a buy with no reportable delivery data"


class TestManualForceSharesReservePathAndRespectsOneShotFinal:
    """trigger_report_for_media_buy_by_id (manual "force" trigger) shares the same
    reserve path and must not create a second final (#1606 point 12).

    Covers: #1606 point 12 (L5)
    """

    @pytest.mark.asyncio
    async def test_manual_trigger_after_final_already_sent_is_a_noop(self, integration_db):
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(env, media_buy_id="mb_manual_after_final")
            scheduler = DeliveryWebhookScheduler()

            # Seed a prior successful final directly (simulating the transition-enqueue
            # having already fired for this buy).
            session = env.get_session()
            DeliveryRepository(session, "t1").create_log(
                log_id=str(uuid4()),
                principal_id="p1",
                media_buy_id=buy.media_buy_id,
                webhook_url="https://example.com/webhook",
                task_type="media_buy_delivery",
                status="success",
                notification_type="final",
            )
            session.commit()

            with patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()) as mock_post:
                success = await scheduler.trigger_report_for_media_buy_by_id(buy.media_buy_id, "t1")

            assert success is False
            mock_post.assert_not_called()


class TestReportingFrequencyKey:
    """The scheduler reads reporting_frequency with a fallback to the legacy
    "frequency" key (#1606 point 11).

    Covers: #1606 point 11 (L5)
    """

    @pytest.mark.asyncio
    async def test_reporting_frequency_key_is_honored(self, integration_db):
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(
                env,
                media_buy_id="mb_reporting_freq",
                raw_request={
                    "reporting_webhook": {"url": "https://example.com/webhook", "reporting_frequency": "daily"}
                },
            )
            scheduler = DeliveryWebhookScheduler()

            with patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()):
                delivered = await scheduler._send_report_for_media_buy(
                    buy, buy.raw_request["reporting_webhook"], env.get_session()
                )

            assert delivered is True

    @pytest.mark.asyncio
    async def test_legacy_frequency_key_still_honored(self, integration_db):
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(
                env,
                media_buy_id="mb_legacy_freq",
                raw_request={"reporting_webhook": {"url": "https://example.com/webhook", "frequency": "daily"}},
            )
            scheduler = DeliveryWebhookScheduler()

            with patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()):
                delivered = await scheduler._send_report_for_media_buy(
                    buy, buy.raw_request["reporting_webhook"], env.get_session()
                )

            assert delivered is True

    @pytest.mark.asyncio
    async def test_unsupported_reporting_frequency_is_skipped(self, integration_db):
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(
                env,
                media_buy_id="mb_hourly_freq",
                raw_request={
                    "reporting_webhook": {"url": "https://example.com/webhook", "reporting_frequency": "hourly"}
                },
            )
            scheduler = DeliveryWebhookScheduler()

            with patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()) as mock_post:
                delivered = await scheduler._send_report_for_media_buy(
                    buy, buy.raw_request["reporting_webhook"], env.get_session()
                )

            assert delivered is False
            mock_post.assert_not_called()


class TestDurableAckFailureReportsNotDelivered:
    """A durable-ack write failure after a successful HTTP send is not
    swallowed — the deliverer must report non-delivery, not a silently-lost
    log write masquerading as success.

    Covers: #1606 point 7 (L2)
    """

    @pytest.mark.asyncio
    async def test_row_stays_reserved_and_delivery_counts_as_error_when_ack_write_fails(self, integration_db):
        """The HTTP POST succeeds, but the durable-ack write (mark_delivery_result)
        raises. The row must NOT be silently marked "success" — it stays
        "reserved" (the write never completed) — and the scheduler must
        propagate the failure as an error, not swallow it as a delivered report."""
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            buy = _make_active_buy(env, media_buy_id="mb_ack_fails")
            scheduler = DeliveryWebhookScheduler()

            with (
                patch.object(scheduler.webhook_service._session, "post", return_value=_ok_response()),
                patch(
                    "src.core.database.repositories.delivery.DeliveryRepository.mark_delivery_result",
                    side_effect=RuntimeError("simulated durable-ack DB failure"),
                ),
                pytest.raises(RuntimeError, match="webhook send failed"),
            ):
                await scheduler._send_report_for_media_buy(
                    buy, buy.raw_request["reporting_webhook"], env.get_session(), force=True
                )

            row = (
                env.get_session()
                .scalars(select(WebhookDeliveryLog).where(WebhookDeliveryLog.media_buy_id == buy.media_buy_id))
                .one()
            )
            assert row.status == "reserved", "the ack write failed — the row must not be masqueraded as success"
            assert row.sequence_number == 1

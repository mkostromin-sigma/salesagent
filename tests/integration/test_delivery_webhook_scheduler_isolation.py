"""Real-DB oracles for delivery webhook scheduler per-buy isolation (#1714 / #1719).

Mirrors ``tests/integration/test_media_buy_status_scheduler.py`` isolation
graders: live PostgreSQL session for listing buys, production
``_send_reports`` entry, outbound HTTP mocked only at ``send_notification``.
Deleting ``on_error`` rollback/metering or the escape seam must redden these.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import OperationalError

import src.core.database.database_session as db_session_mod
import src.services.delivery_webhook_scheduler as delivery_mod
from src.core.database.database_session import get_db_session
from src.core.database.models import MediaBuy, Principal, Tenant
from src.core.metrics import scheduler_isolation_errors
from src.services.delivery_webhook_scheduler import DELIVERY_BATCH_SUMMARY_PREFIX, DeliveryWebhookScheduler
from tests.helpers.scheduler_isolation import counter_value, summary_lines


def _create_tenant_principal(tenant_id: str, principal_id: str) -> None:
    with get_db_session() as session:
        session.add(
            Tenant(
                tenant_id=tenant_id,
                name=f"Tenant {tenant_id}",
                subdomain=tenant_id.replace("_", "-")[:63],
                ad_server="mock",
            )
        )
        session.add(
            Principal(
                tenant_id=tenant_id,
                principal_id=principal_id,
                name=f"Principal {principal_id}",
                platform_mappings={"mock": {"advertiser_id": "adv_123"}},
                access_token=f"token-{tenant_id}",
            )
        )
        session.commit()


def _create_active_buy_with_webhook(tenant_id: str, principal_id: str, media_buy_id: str) -> None:
    now = datetime.now(UTC)
    with get_db_session() as session:
        session.add(
            MediaBuy(
                media_buy_id=media_buy_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                order_name=f"Order {media_buy_id}",
                advertiser_name="Test Advertiser",
                start_date=(now - timedelta(days=7)).date(),
                end_date=(now + timedelta(days=7)).date(),
                status="active",
                raw_request={
                    "packages": [{"product_id": "prod_1"}],
                    "reporting_webhook": {
                        "url": f"https://example.com/hook/{media_buy_id}",
                        "frequency": "daily",
                    },
                },
            )
        )
        session.commit()


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_delivery_send_reports_isolates_one_failure_against_live_session(integration_db):
    """One failing webhook POST must not stop siblings; only the failing buy is metered."""
    tenants = [
        ("tenant_deliv_iso_a_1714", "p_a", "mb_deliv_a"),
        ("tenant_deliv_iso_fail_1714", "p_fail", "mb_deliv_fail"),
        ("tenant_deliv_iso_b_1714", "p_b", "mb_deliv_b"),
    ]
    for tenant_id, principal_id, media_buy_id in tenants:
        _create_tenant_principal(tenant_id, principal_id)
        _create_active_buy_with_webhook(tenant_id, principal_id, media_buy_id)

    fail_buy = "mb_deliv_fail"
    sent: list[str] = []

    async def fake_send_notification(*_args, **kwargs):
        metadata = kwargs.get("metadata") or {}
        media_buy_id = metadata.get("media_buy_id")
        if media_buy_id == fail_buy:
            raise ConnectionError("webhook POST failed")
        sent.append(media_buy_id)
        return True

    scheduler = DeliveryWebhookScheduler()
    scheduler_isolation_errors.clear()
    fail_before = counter_value("delivery_webhook", "tenant_deliv_iso_fail_1714", "transport")
    ok_a_before = counter_value("delivery_webhook", "tenant_deliv_iso_a_1714", "transport")

    with (
        patch.object(
            scheduler.webhook_service,
            "send_notification",
            new_callable=AsyncMock,
            side_effect=fake_send_notification,
        ) as mock_send,
        patch.object(delivery_mod.logger, "error") as mock_error,
        patch.object(delivery_mod.logger, "info") as mock_info,
    ):
        await scheduler._send_reports()

    assert set(sent) == {"mb_deliv_a", "mb_deliv_b"}
    assert mock_send.await_count == 3
    assert mock_error.call_count == 1
    assert "media_buy_id=mb_deliv_fail" in mock_error.call_args.args[0]
    assert mock_error.call_args.kwargs.get("exc_info") is True

    info_summaries = summary_lines(mock_info, DELIVERY_BATCH_SUMMARY_PREFIX)
    assert len(info_summaries) == 1
    assert "2 sent, 1 errors" in info_summaries[0]

    assert counter_value("delivery_webhook", "tenant_deliv_iso_fail_1714", "transport") == fail_before + 1
    assert counter_value("delivery_webhook", "tenant_deliv_iso_a_1714", "transport") == ok_a_before


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_delivery_invalidated_error_arms_real_breaker(integration_db):
    """Real get_db_session CM must arm the breaker on escaped invalidated errors."""
    tenant_id = "tenant_deliv_breaker_1714"
    principal_id = "p_breaker"
    media_buy_id = "mb_deliv_breaker"
    _create_tenant_principal(tenant_id, principal_id)
    _create_active_buy_with_webhook(tenant_id, principal_id, media_buy_id)

    db_session_mod.reset_health_state()
    assert db_session_mod._is_healthy is True

    scheduler = DeliveryWebhookScheduler()

    async def _raise_invalidated(*_a, **_k):
        raise OperationalError("SELECT 1", {}, Exception("gone"), connection_invalidated=True)

    try:
        with patch.object(
            scheduler,
            "_send_report_for_media_buy",
            new_callable=AsyncMock,
            side_effect=_raise_invalidated,
        ):
            await scheduler._send_reports()
        assert db_session_mod._is_healthy is False
    finally:
        db_session_mod.reset_health_state()

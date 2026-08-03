"""Real-DB oracles for delivery webhook scheduler per-buy isolation (#1714 / #1719).

Mirrors ``tests/integration/test_media_buy_status_scheduler.py`` isolation
graders: live PostgreSQL for listing buys, production ``_send_reports`` entry,
outbound HTTP mocked only at ``send_notification``. Deleting ``on_error``
rollback/metering or the escape seam must redden these.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import src.services.delivery_webhook_scheduler as delivery_mod
from src.core.metrics import scheduler_isolation_errors
from src.services.delivery_webhook_scheduler import DELIVERY_BATCH_SUMMARY_PREFIX, DeliveryWebhookScheduler
from tests.factories import MediaBuyFactory, PrincipalFactory, TenantFactory
from tests.harness._base import IntegrationEnv
from tests.helpers.scheduler_isolation import (
    assert_escaped_invalidation_arms_breaker,
    counter_value,
    invalidated_operational_error,
    patch_scheduler_send_notification,
    summary_lines,
)


def _seed_active_buy_with_webhook(tenant_id: str, principal_id: str, media_buy_id: str) -> None:
    """Factory-seed one active buy with a reporting webhook (session via IntegrationEnv)."""
    now = datetime.now(UTC)
    tenant = TenantFactory(tenant_id=tenant_id)
    principal = PrincipalFactory(tenant=tenant, principal_id=principal_id)
    MediaBuyFactory(
        tenant=tenant,
        principal=principal,
        media_buy_id=media_buy_id,
        status="active",
        start_date=(now - timedelta(days=7)).date(),
        end_date=(now + timedelta(days=7)).date(),
        raw_request={
            "packages": [{"product_id": "prod_1"}],
            "reporting_webhook": {
                "url": f"https://example.com/hook/{media_buy_id}",
                "frequency": "daily",
            },
        },
    )


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_delivery_send_reports_isolates_one_failure_against_live_session(integration_db):
    """One failing webhook POST must not stop siblings; only the failing buy is metered."""
    tenants = [
        ("tenant_deliv_iso_a_1714", "p_a", "mb_deliv_a"),
        ("tenant_deliv_iso_fail_1714", "p_fail", "mb_deliv_fail"),
        ("tenant_deliv_iso_b_1714", "p_b", "mb_deliv_b"),
    ]
    with IntegrationEnv(tenant_id="tenant_deliv_iso_seed_1714", principal_id="p_seed"):
        for tenant_id, principal_id, media_buy_id in tenants:
            _seed_active_buy_with_webhook(tenant_id, principal_id, media_buy_id)

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
        patch_scheduler_send_notification(scheduler, fake_send_notification) as mock_send,
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
    with IntegrationEnv(tenant_id="tenant_deliv_breaker_1714", principal_id="p_breaker"):
        _seed_active_buy_with_webhook("tenant_deliv_breaker_1714", "p_breaker", "mb_deliv_breaker")

    scheduler = DeliveryWebhookScheduler()

    async def _raise_invalidated(*_a, **_k):
        raise invalidated_operational_error()

    async def _run() -> None:
        with patch.object(
            scheduler,
            "_send_report_for_media_buy",
            new_callable=AsyncMock,
            side_effect=_raise_invalidated,
        ):
            await scheduler._send_reports()

    await assert_escaped_invalidation_arms_breaker(_run)

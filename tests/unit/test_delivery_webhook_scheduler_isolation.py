"""Unit tests: delivery webhook scheduler per-buy isolation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.metrics import scheduler_isolation_errors
from src.services.delivery_webhook_scheduler import DeliveryWebhookScheduler


def _buy(tenant_id: str, media_buy_id: str) -> MagicMock:
    buy = MagicMock()
    buy.tenant_id = tenant_id
    buy.principal_id = f"p-{media_buy_id}"
    buy.media_buy_id = media_buy_id
    buy.raw_request = {"reporting_webhook": {"url": "https://example.com/hook", "frequency": "daily"}}
    buy.status = "active"
    return buy


@pytest.mark.asyncio
async def test_delivery_send_reports_isolates_one_failure_and_meters_once():
    """Inject a failure into one ``_send_report_for_media_buy``; siblings still send."""
    buys = [
        _buy("tenant-ok-a", "mb_a"),
        _buy("tenant-fail", "mb_fail"),
        _buy("tenant-ok-b", "mb_b"),
    ]
    sent: list[str] = []

    session = MagicMock()
    nested = MagicMock()
    session.begin_nested.return_value = nested
    nested.__enter__ = MagicMock(return_value=nested)
    nested.__exit__ = MagicMock(side_effect=lambda *_a, **_k: False)

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    scheduler = DeliveryWebhookScheduler()

    async def _send(media_buy, reporting_webhook, sess, force=False):
        if media_buy.media_buy_id == "mb_fail":
            raise RuntimeError("send failed")
        sent.append(media_buy.media_buy_id)

    scheduler_isolation_errors.clear()
    metric_before = scheduler_isolation_errors.labels(
        scheduler="delivery_webhook",
        tenant_id="tenant-fail",
        error_type="model_error",
    )._value.get()

    with (
        patch("src.services.delivery_webhook_scheduler.get_db_session", return_value=cm),
        patch(
            "src.services.delivery_webhook_scheduler.MediaBuyRepository.get_all_by_statuses",
            return_value=buys,
        ),
        patch.object(scheduler, "_send_report_for_media_buy", new_callable=AsyncMock, side_effect=_send),
        patch.object(scheduler.webhook_service, "send_notification", new_callable=AsyncMock),
    ):
        await scheduler._send_reports()

    assert sent == ["mb_a", "mb_b"]
    metric_after = scheduler_isolation_errors.labels(
        scheduler="delivery_webhook",
        tenant_id="tenant-fail",
        error_type="model_error",
    )._value.get()
    assert metric_after == metric_before + 1

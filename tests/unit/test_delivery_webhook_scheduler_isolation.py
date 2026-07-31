"""Unit tests: delivery webhook scheduler per-buy isolation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from sqlalchemy.exc import OperationalError

import src.core.database.database_session as db_session_mod
import src.services.delivery_webhook_scheduler as delivery_mod
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


def _session_cm(session: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_delivery_send_reports_isolates_one_failure_and_meters_once():
    """Inject a transport failure into one send; siblings still send and are not metered."""
    buys = [
        _buy("tenant-ok-a", "mb_a"),
        _buy("tenant-fail", "mb_fail"),
        _buy("tenant-ok-b", "mb_b"),
    ]
    sent: list[str] = []
    session = MagicMock()

    scheduler = DeliveryWebhookScheduler()

    async def _send(media_buy, reporting_webhook, sess, force=False):
        if media_buy.media_buy_id == "mb_fail":
            raise RequestsConnectionError("send failed")
        sent.append(media_buy.media_buy_id)

    scheduler_isolation_errors.clear()
    fail_before = scheduler_isolation_errors.labels(
        scheduler="delivery_webhook",
        tenant_id="tenant-fail",
        error_type="transport",
    )._value.get()
    ok_a_before = scheduler_isolation_errors.labels(
        scheduler="delivery_webhook",
        tenant_id="tenant-ok-a",
        error_type="transport",
    )._value.get()
    ok_b_before = scheduler_isolation_errors.labels(
        scheduler="delivery_webhook",
        tenant_id="tenant-ok-b",
        error_type="transport",
    )._value.get()

    with (
        patch("src.services.delivery_webhook_scheduler.get_db_session", return_value=_session_cm(session)),
        patch(
            "src.services.delivery_webhook_scheduler.MediaBuyRepository.get_all_by_statuses",
            return_value=buys,
        ),
        patch.object(scheduler, "_send_report_for_media_buy", new_callable=AsyncMock, side_effect=_send),
        patch.object(scheduler.webhook_service, "send_notification", new_callable=AsyncMock),
        patch.object(delivery_mod.logger, "error") as mock_error,
        patch.object(delivery_mod.logger, "info") as mock_info,
    ):
        await scheduler._send_reports()

    assert sent == ["mb_a", "mb_b"]
    assert session.rollback.call_count == 1
    assert mock_error.call_count == 1
    assert mock_error.call_args.kwargs.get("exc_info") is True
    assert "tenant_id=tenant-fail" in mock_error.call_args.args[0]

    info_summaries = [
        c.args[0]
        for c in mock_info.call_args_list
        if c.args and "Daily delivery report batch complete:" in str(c.args[0])
    ]
    assert len(info_summaries) == 1
    assert "2 sent, 1 errors" in info_summaries[0]

    assert (
        scheduler_isolation_errors.labels(
            scheduler="delivery_webhook",
            tenant_id="tenant-fail",
            error_type="transport",
        )._value.get()
        == fail_before + 1
    )
    assert (
        scheduler_isolation_errors.labels(
            scheduler="delivery_webhook",
            tenant_id="tenant-ok-a",
            error_type="transport",
        )._value.get()
        == ok_a_before
    )
    assert (
        scheduler_isolation_errors.labels(
            scheduler="delivery_webhook",
            tenant_id="tenant-ok-b",
            error_type="transport",
        )._value.get()
        == ok_b_before
    )


@pytest.mark.asyncio
async def test_delivery_connection_invalidated_arms_breaker():
    """Adoption oracle: delivery routes through the seam and escapes to the breaker."""
    db_session_mod.reset_health_state()
    assert db_session_mod._is_healthy is True

    buy = _buy("t-breaker", "mb1")
    session = MagicMock()

    def fake_get_db_session():
        class _CM:
            def __enter__(self):
                return session

            def __exit__(self, exc_type, exc, tb):
                if exc_type is not None and issubclass(exc_type, db_session_mod.CONNECTION_ERROR_TYPES):
                    db_session_mod._is_healthy = False
                    db_session_mod._last_health_check = 0.0
                return False

        return _CM()

    scheduler = DeliveryWebhookScheduler()

    async def _raise_invalidated(*_a, **_k):
        raise OperationalError("SELECT 1", {}, Exception("gone"), connection_invalidated=True)

    try:
        with (
            patch("src.services.delivery_webhook_scheduler.get_db_session", side_effect=fake_get_db_session),
            patch(
                "src.services.delivery_webhook_scheduler.MediaBuyRepository.get_all_by_statuses",
                return_value=[buy],
            ),
            patch.object(
                scheduler, "_send_report_for_media_buy", new_callable=AsyncMock, side_effect=_raise_invalidated
            ),
        ):
            await scheduler._send_reports()

        assert db_session_mod._is_healthy is False
    finally:
        db_session_mod.reset_health_state()

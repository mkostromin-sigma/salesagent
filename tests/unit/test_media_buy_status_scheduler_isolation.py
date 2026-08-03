"""Unit tests: status scheduler adopts run_isolated_batch and breaker escape."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

import src.services.media_buy_status_scheduler as status_mod
from src.core.metrics import scheduler_isolation_errors
from src.services.media_buy_status_scheduler import STATUS_BATCH_SUMMARY_PREFIX, MediaBuyStatusScheduler
from tests.helpers.scheduler_isolation import counter_value, summary_lines


@pytest.mark.asyncio
async def test_status_scheduler_connection_invalidated_reraises():
    """Adoption oracle: invalidated OperationalError re-raises out of the batch.

    Breaker arming against the real ``get_db_session`` CM is graded by the
    integration twin; this unit test pins the re-raise half of the seam via
    the branch-distinguishing outer log (not the per-item isolate log).
    """
    buy = MagicMock()
    buy.tenant_id = "t-breaker"
    buy.principal_id = "p1"
    buy.media_buy_id = "mb1"
    buy.status = "active"
    buy.start_time = datetime(2020, 1, 1, tzinfo=UTC)
    buy.end_time = datetime(2020, 1, 2, tzinfo=UTC)
    buy.start_date = None
    buy.end_date = None

    session = MagicMock()
    nested = MagicMock()
    session.begin_nested.return_value = nested
    nested.__enter__ = MagicMock(return_value=nested)
    nested.__exit__ = MagicMock(side_effect=lambda *_a, **_k: False)

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    scheduler = MediaBuyStatusScheduler()

    def _raise_invalidated(_media_buy, _now, _session):
        raise OperationalError("SELECT 1", {}, Exception("gone"), connection_invalidated=True)

    scheduler_isolation_errors.clear()
    metric_before = counter_value("media_buy_status", "t-breaker", "db_error")

    with (
        patch("src.services.media_buy_status_scheduler.get_db_session", return_value=cm),
        patch(
            "src.services.media_buy_status_scheduler.MediaBuyRepository.get_all_by_statuses",
            return_value=[buy],
        ),
        patch.object(scheduler, "_compute_new_status", side_effect=_raise_invalidated),
        patch.object(status_mod.logger, "error") as mock_error,
    ):
        await scheduler._update_statuses()

    error_msgs = [str(c.args[0]) for c in mock_error.call_args_list if c.args]
    assert any("Failed to update media buy statuses" in msg for msg in error_msgs)
    assert not any("Error updating media buy status" in msg for msg in error_msgs)
    assert counter_value("media_buy_status", "t-breaker", "db_error") == metric_before


@pytest.mark.asyncio
async def test_status_send_isolates_one_failure_and_meters_once():
    """Status twin of the delivery metering oracle — pins scheduler= allowlist literal."""
    buys = []
    for mid, tenant in (("mb_a", "tenant-ok-a"), ("mb_fail", "tenant-fail"), ("mb_b", "tenant-ok-b")):
        buy = MagicMock()
        buy.tenant_id = tenant
        buy.principal_id = f"p-{mid}"
        buy.media_buy_id = mid
        buy.status = "active"
        buy.start_time = datetime(2020, 1, 1, tzinfo=UTC)
        buy.end_time = datetime(2020, 1, 2, tzinfo=UTC)
        buy.start_date = None
        buy.end_date = None
        buys.append(buy)

    session = MagicMock()
    nested = MagicMock()
    session.begin_nested.return_value = nested
    nested.__enter__ = MagicMock(return_value=nested)
    nested.__exit__ = MagicMock(side_effect=lambda *_a, **_k: False)

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)

    scheduler = MediaBuyStatusScheduler()
    flipped: list[str] = []

    def _compute(media_buy, _now, _session):
        if media_buy.media_buy_id == "mb_fail":
            raise OperationalError("SELECT 1", {}, Exception("timeout"))
        flipped.append(media_buy.media_buy_id)
        return "completed"

    scheduler_isolation_errors.clear()
    fail_before = counter_value("media_buy_status", "tenant-fail", "db_error")

    with (
        patch("src.services.media_buy_status_scheduler.get_db_session", return_value=cm),
        patch(
            "src.services.media_buy_status_scheduler.MediaBuyRepository.get_all_by_statuses",
            return_value=buys,
        ),
        patch.object(scheduler, "_compute_new_status", side_effect=_compute),
        patch.object(status_mod.logger, "error") as mock_error,
        patch.object(status_mod.logger, "info") as mock_info,
    ):
        await scheduler._update_statuses()

    assert flipped == ["mb_a", "mb_b"]
    assert mock_error.call_count == 1
    assert mock_error.call_args.kwargs.get("exc_info") is True
    assert "tenant_id=tenant-fail" in mock_error.call_args.args[0]

    info_summaries = summary_lines(mock_info, STATUS_BATCH_SUMMARY_PREFIX)
    assert len(info_summaries) == 1
    assert "2 updated, 1 errors" in info_summaries[0]

    assert counter_value("media_buy_status", "tenant-fail", "db_error") == fail_before + 1
    # Siblings must not be metered.
    assert counter_value("media_buy_status", "tenant-ok-a", "db_error") == 0

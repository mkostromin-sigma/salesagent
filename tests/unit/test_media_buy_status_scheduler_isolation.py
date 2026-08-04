"""Unit tests: status scheduler per-buy SAVEPOINT isolation and breaker escape.

Durable-commit and sibling-survival guarantees are graded by the real-Postgres
twin ``test_raising_buy_does_not_abort_remaining_status_flips``
(``tests/integration/test_media_buy_status_scheduler.py``) — these unit tests
pin only what that twin cannot cheaply express without a real DB: the
re-raise/escape control flow routing to the outer log, and the
``scheduler="media_buy_status"`` metric label.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

import src.services.media_buy_status_scheduler as status_mod
from src.core.metrics import scheduler_isolation_errors
from src.services.media_buy_status_scheduler import MediaBuyStatusScheduler
from tests.helpers.scheduler_isolation import (
    counter_value,
    mock_get_db_session_cm,
    mock_media_buy,
    mock_savepoint_session,
)


@pytest.mark.asyncio
async def test_status_scheduler_connection_invalidated_reraises():
    """Adoption oracle: invalidated OperationalError re-raises out of the batch.

    Breaker arming against the real ``get_db_session`` CM is graded by the
    integration twin; this unit test pins the re-raise half via the
    branch-distinguishing outer log (not the per-item isolate log).
    """
    buy = mock_media_buy(media_buy_id="mb1", tenant_id="t-breaker")
    session = mock_savepoint_session()
    cm = mock_get_db_session_cm(session)

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
    """Pins the ``scheduler="media_buy_status"`` metric label and per-tenant
    non-metering of siblings — not durable commit (mocked session/objects
    can't grade that; the real-DB twin does)."""
    buys = [
        mock_media_buy(media_buy_id="mb_a", tenant_id="tenant-ok-a", principal_id="p-mb_a"),
        mock_media_buy(media_buy_id="mb_fail", tenant_id="tenant-fail", principal_id="p-mb_fail"),
        mock_media_buy(media_buy_id="mb_b", tenant_id="tenant-ok-b", principal_id="p-mb_b"),
    ]
    session = mock_savepoint_session()
    cm = mock_get_db_session_cm(session)

    scheduler = MediaBuyStatusScheduler()

    def _compute(media_buy, _now, _session):
        if media_buy.media_buy_id == "mb_fail":
            raise OperationalError("SELECT 1", {}, Exception("timeout"))
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
    ):
        await scheduler._update_statuses()

    assert mock_error.call_count == 1
    assert mock_error.call_args.kwargs.get("exc_info") is True
    assert "tenant_id=tenant-fail" in mock_error.call_args.args[0]

    assert counter_value("media_buy_status", "tenant-fail", "db_error") == fail_before + 1
    # Siblings must not be metered.
    assert counter_value("media_buy_status", "tenant-ok-a", "db_error") == 0

"""Unit tests: status scheduler adopts run_isolated_batch and breaker escape."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

import src.core.database.database_session as db_session_mod
from src.services.media_buy_status_scheduler import MediaBuyStatusScheduler


@pytest.mark.asyncio
async def test_status_scheduler_connection_invalidated_arms_breaker():
    """Adoption oracle: invalidated OperationalError must reach the breaker.

    Hand-rolling the loop (never importing the helper) would leave
    ``_is_healthy`` True; routing through ``run_isolated_batch`` re-raises
    and the session CM arms the process-global breaker.
    """
    db_session_mod.reset_health_state()
    assert db_session_mod._is_healthy is True

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

    scheduler = MediaBuyStatusScheduler()

    def _raise_invalidated(_media_buy, _now, _session):
        raise OperationalError("SELECT 1", {}, Exception("gone"), connection_invalidated=True)

    with (
        patch("src.services.media_buy_status_scheduler.get_db_session", side_effect=fake_get_db_session),
        patch(
            "src.services.media_buy_status_scheduler.MediaBuyRepository.get_all_by_statuses",
            return_value=[buy],
        ),
        patch.object(scheduler, "_compute_new_status", side_effect=_raise_invalidated),
    ):
        await scheduler._update_statuses()

    assert db_session_mod._is_healthy is False
    db_session_mod.reset_health_state()

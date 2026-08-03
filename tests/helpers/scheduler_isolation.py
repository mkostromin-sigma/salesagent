"""Shared helpers for scheduler isolation test oracles."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import OperationalError


def summary_lines(mock_logger: MagicMock, prefix: str) -> list[str]:
    """Extract batch-summary log lines that start with ``{prefix}:``.

    Matches the production ``log_batch_summary`` format
    (``f"{prefix}: {processed} …"``). Prefer the production prefix constants
    (``STATUS_BATCH_SUMMARY_PREFIX`` / ``DELIVERY_BATCH_SUMMARY_PREFIX``).
    """
    needle = f"{prefix}:"
    return [call.args[0] for call in mock_logger.call_args_list if call.args and needle in str(call.args[0])]


def counter_value(scheduler: str, tenant_id: str, error_type: str) -> float:
    """Read ``scheduler_isolation_errors`` gauge for one label triple."""
    from src.core.metrics import scheduler_isolation_errors

    return scheduler_isolation_errors.labels(
        scheduler=scheduler,
        tenant_id=tenant_id,
        error_type=error_type,
    )._value.get()


def invalidated_operational_error() -> OperationalError:
    """Connection-invalidated OperationalError used by breaker-arm oracles."""
    return OperationalError("SELECT 1", {}, Exception("gone"), connection_invalidated=True)


async def assert_escaped_invalidation_arms_breaker(run: Callable[[], Awaitable[None]]) -> None:
    """Run ``run`` and assert a real ``get_db_session`` CM arms the DB breaker.

    Shared by status and delivery scheduler integration oracles so the
    reset / assert / finally pattern is not duplicated (R0801).
    """
    import src.core.database.database_session as db_session_mod

    db_session_mod.reset_health_state()
    assert db_session_mod._is_healthy is True
    try:
        await run()
        assert db_session_mod._is_healthy is False
    finally:
        db_session_mod.reset_health_state()


@contextmanager
def patch_scheduler_send_notification(scheduler: Any, side_effect: Any) -> Iterator[AsyncMock]:
    """Patch ``scheduler.webhook_service.send_notification`` as an AsyncMock.

    Shared by delivery scheduler integration tests so the patch boilerplate
    is not duplicated across isolation / force-trigger modules (R0801).
    """
    with patch.object(
        scheduler.webhook_service,
        "send_notification",
        new_callable=AsyncMock,
        side_effect=side_effect,
    ) as mock_send:
        yield mock_send

"""Unit tests for scheduler per-item isolation helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError

from src.services.isolated_batch import (
    BatchOutcome,
    default_escape_isolation,
    log_batch_summary,
    media_buy_context,
    run_isolated_batch,
    run_isolated_batch_async,
)


def _ctx(n: int):
    """SchedulerItemContext-shaped stand-in for int-keyed unit loops."""
    return media_buy_context(MagicMock(tenant_id=f"t{n}", principal_id=f"p{n}", media_buy_id=f"mb{n}"))


def test_run_isolated_batch_counts_success_and_isolates_errors():
    seen_items: list[int] = []
    errors: list[tuple[str, str]] = []

    def handle(item: int) -> bool:
        seen_items.append(item)
        if item == 2:
            raise ValueError("boom")
        return item % 2 == 1

    def on_error(ctx, exc: Exception) -> None:
        errors.append((ctx.media_buy_id, type(exc).__name__))

    outcome = run_isolated_batch(
        [1, 2, 3],
        handle,
        item_context=_ctx,
        on_error=on_error,
    )

    assert seen_items == [1, 2, 3]
    assert isinstance(outcome, BatchOutcome)
    assert outcome.processed == 2  # 1 and 3 truthy; 2 raised
    assert outcome.errors == 1
    assert outcome.seen == 3
    assert errors == [("mb2", "ValueError")]


def test_run_isolated_batch_isolates_operational_error_without_invalidated():
    """Bare OperationalError (e.g. QueryCanceled) must stay isolatable."""
    seen_items: list[int] = []

    def handle(item: int) -> bool:
        seen_items.append(item)
        if item == 1:
            raise OperationalError("SELECT 1", {}, Exception("statement timeout"))
        return True

    outcome = run_isolated_batch(
        [1, 2],
        handle,
        item_context=_ctx,
        on_error=lambda _ctx, _exc: None,
    )

    assert seen_items == [1, 2]
    assert outcome.processed == 1
    assert outcome.errors == 1
    assert outcome.seen == 2


def test_run_isolated_batch_reraises_operational_error_when_connection_invalidated():
    def handle(_item: int) -> bool:
        raise OperationalError("SELECT 1", {}, Exception("db down"), connection_invalidated=True)

    with pytest.raises(OperationalError):
        run_isolated_batch(
            [1],
            handle,
            item_context=_ctx,
            on_error=lambda _ctx, _exc: None,
        )


def test_run_isolated_batch_reraises_disconnection_error():
    def handle(_item: int) -> bool:
        raise DisconnectionError("gone")

    with pytest.raises(DisconnectionError):
        run_isolated_batch(
            [1],
            handle,
            item_context=_ctx,
            on_error=lambda _ctx, _exc: None,
        )


def test_run_isolated_batch_reraises_interface_error_when_connection_invalidated():
    def handle(_item: int) -> bool:
        raise InterfaceError("closed", {}, Exception("gone"), connection_invalidated=True)

    with pytest.raises(InterfaceError):
        run_isolated_batch(
            [1],
            handle,
            item_context=_ctx,
            on_error=lambda _ctx, _exc: None,
        )


def test_default_escape_isolation_predicate():
    plain = OperationalError("SELECT 1", {}, Exception("timeout"))
    invalidated = OperationalError("SELECT 1", {}, Exception("gone"), connection_invalidated=True)
    iface_dead = InterfaceError("closed", {}, Exception("gone"), connection_invalidated=True)
    iface_plain = InterfaceError("x", {}, Exception("x"))
    assert default_escape_isolation(plain) is False
    assert default_escape_isolation(invalidated) is True
    assert default_escape_isolation(DisconnectionError("gone")) is True
    assert default_escape_isolation(iface_dead) is True
    assert default_escape_isolation(iface_plain) is False
    assert default_escape_isolation(ValueError("x")) is False


def test_raising_on_error_still_visits_all_items():
    seen_items: list[int] = []

    def handle(item: int) -> bool:
        seen_items.append(item)
        if item == 1:
            raise ValueError("original")
        return True

    def on_error(_ctx, _exc: Exception) -> None:
        raise KeyError("handler blew up")

    outcome = run_isolated_batch(
        [1, 2, 3],
        handle,
        item_context=_ctx,
        on_error=on_error,
    )

    assert seen_items == [1, 2, 3]
    assert outcome.processed == 2
    assert outcome.errors == 1


def test_raising_escape_predicate_is_isolated():
    """A broken escape predicate must not abort remaining items."""
    seen_items: list[int] = []

    def handle(item: int) -> bool:
        seen_items.append(item)
        if item == 1:
            raise ValueError("original")
        return True

    outcome = run_isolated_batch(
        [1, 2, 3],
        handle,
        item_context=_ctx,
        on_error=lambda _c, _e: None,
        escape_isolation=lambda _exc: (_ for _ in ()).throw(RuntimeError("predicate blew up")),
    )

    assert seen_items == [1, 2, 3]
    assert outcome.processed == 2
    assert outcome.errors == 1


def test_savepoint_release_failure_not_counted_as_processed():
    """BLOCKER oracle: release-time failure must not tally processed.

    Buggy code increments inside the savepoint scope and reports
    processed==errors; fixed code keeps processed at 0 so all-fail WARNING
    can fire.
    """
    session = MagicMock()
    nested = MagicMock()
    session.begin_nested.return_value = nested
    nested.__enter__ = MagicMock(return_value=nested)
    nested.__exit__ = MagicMock(
        side_effect=OperationalError("UPDATE …", {}, Exception("QueryCanceled")),
    )

    def handle(_item: int) -> bool:
        return True

    errors: list[str] = []

    outcome = run_isolated_batch(
        [1, 2, 3],
        handle,
        item_context=_ctx,
        on_error=lambda ctx, _exc: errors.append(ctx.media_buy_id),
        session=session,
    )

    assert outcome.seen == 3
    assert outcome.processed == 0
    assert outcome.errors == 3
    assert errors == ["mb1", "mb2", "mb3"]


def test_item_context_ids_reach_on_error_after_flush_failure():
    """Pre-capture invariant: on_error receives ids captured before handle_item."""
    session = MagicMock()
    nested = MagicMock()
    session.begin_nested.return_value = nested
    nested.__enter__ = MagicMock(return_value=nested)
    nested.__exit__ = MagicMock(return_value=False)

    received: list[tuple[str, str]] = []

    def handle(_item: int) -> bool:
        raise OperationalError("flush", {}, Exception("StringDataRightTruncation"))

    def on_error(ctx, exc: Exception) -> None:
        received.append((ctx.tenant_id, type(exc).__name__))

    outcome = run_isolated_batch(
        [1],
        handle,
        item_context=_ctx,
        on_error=on_error,
        session=session,
    )

    assert outcome.errors == 1
    assert received == [("t1", "OperationalError")]


def test_raising_item_context_is_isolated_not_batch_abort():
    seen_items: list[int] = []

    def handle(item: int) -> bool:
        seen_items.append(item)
        return True

    def bad_context(item: int):
        if item == 1:
            raise RuntimeError("ctx failed")
        return _ctx(item)

    outcome = run_isolated_batch(
        [1, 2],
        handle,
        item_context=bad_context,
        on_error=lambda _c, _e: None,
    )

    assert seen_items == [2]
    assert outcome.seen == 2
    assert outcome.processed == 1
    assert outcome.errors == 1


def test_falsy_handler_return_is_graded():
    """handle_item returning False is 'handled, no tally'."""
    outcome = run_isolated_batch(
        [1, 2],
        handle_item=lambda _i: False,
        item_context=_ctx,
        on_error=lambda _c, _e: None,
    )
    assert outcome.processed == 0
    assert outcome.errors == 0
    assert outcome.seen == 2


def test_log_batch_summary_warning_when_every_seen_item_failed():
    batch_logger = MagicMock()
    log_batch_summary(batch_logger, "Batch", processed=0, errors=3, seen=3, success_label="updated")
    batch_logger.warning.assert_called_once_with("Batch: 0 updated, 3 errors")
    batch_logger.info.assert_not_called()


def test_log_batch_summary_info_when_some_noop_and_one_error():
    """Legitimate False returns must not escalate a single failure to WARNING."""
    batch_logger = MagicMock()
    log_batch_summary(batch_logger, "Batch", processed=0, errors=1, seen=5, success_label="updated")
    batch_logger.info.assert_called_once_with("Batch: 0 updated, 1 errors")
    batch_logger.warning.assert_not_called()


def test_log_batch_summary_suppresses_quiet_tick():
    batch_logger = MagicMock()
    log_batch_summary(
        batch_logger,
        "Batch",
        processed=0,
        errors=0,
        seen=0,
        suppress_when_quiet=True,
    )
    batch_logger.info.assert_not_called()
    batch_logger.warning.assert_not_called()


def test_log_batch_summary_success_only_info():
    batch_logger = MagicMock()
    log_batch_summary(batch_logger, "Batch", processed=2, errors=0, seen=2, success_label="updated")
    batch_logger.info.assert_called_once_with("Batch: 2 updated, 0 errors")


@pytest.mark.asyncio
async def test_run_isolated_batch_async_isolates_operational_error_without_invalidated():
    async def handle(item: int) -> bool:
        if item == 1:
            raise OperationalError("SELECT 1", {}, Exception("statement timeout"))
        return True

    outcome = await run_isolated_batch_async(
        [1, 2],
        handle,
        item_context=_ctx,
        on_error=lambda _ctx, _exc: None,
    )

    assert outcome.processed == 1
    assert outcome.errors == 1


@pytest.mark.asyncio
async def test_run_isolated_batch_async_reraises_operational_error_when_invalidated():
    async def handle(_item: int) -> bool:
        raise OperationalError("SELECT 1", {}, Exception("db down"), connection_invalidated=True)

    with pytest.raises(OperationalError):
        await run_isolated_batch_async(
            [1],
            handle,
            item_context=_ctx,
            on_error=lambda _ctx, _exc: None,
        )


@pytest.mark.asyncio
async def test_run_isolated_batch_async_reraises_disconnection_error():
    async def handle(_item: int) -> bool:
        raise DisconnectionError("gone")

    with pytest.raises(DisconnectionError):
        await run_isolated_batch_async(
            [1],
            handle,
            item_context=_ctx,
            on_error=lambda _ctx, _exc: None,
        )


@pytest.mark.asyncio
async def test_run_isolated_batch_async_mirrors_sync():
    seen_items: list[int] = []
    errors: list[tuple[str, str]] = []

    async def handle(item: int) -> bool:
        seen_items.append(item)
        if item == 1:
            raise RuntimeError("x")
        return True

    def on_error(ctx, exc: Exception) -> None:
        errors.append((ctx.media_buy_id, type(exc).__name__))

    outcome = await run_isolated_batch_async(
        [1, 2],
        handle,
        item_context=_ctx,
        on_error=on_error,
    )

    assert seen_items == [1, 2]
    assert outcome.processed == 1
    assert outcome.errors == 1
    assert errors == [("mb1", "RuntimeError")]


@pytest.mark.asyncio
async def test_raising_on_error_async_still_visits_all_items():
    seen_items: list[int] = []

    async def handle(item: int) -> bool:
        seen_items.append(item)
        if item == 2:
            raise ValueError("original")
        return True

    def on_error(_ctx, _exc: Exception) -> None:
        raise RuntimeError("handler blew up")

    outcome = await run_isolated_batch_async(
        [1, 2, 3],
        handle,
        item_context=_ctx,
        on_error=on_error,
    )

    assert seen_items == [1, 2, 3]
    assert outcome.processed == 2
    assert outcome.errors == 1


@pytest.mark.asyncio
async def test_async_savepoint_release_failure_not_counted_as_processed():
    session = MagicMock()
    nested = MagicMock()
    session.begin_nested.return_value = nested
    nested.__enter__ = MagicMock(return_value=nested)
    nested.__exit__ = MagicMock(
        side_effect=OperationalError("UPDATE …", {}, Exception("QueryCanceled")),
    )

    async def handle(_item: int) -> bool:
        return True

    outcome = await run_isolated_batch_async(
        [1, 2, 3],
        handle,
        item_context=_ctx,
        on_error=lambda _c, _e: None,
        session=session,
    )

    assert outcome.processed == 0
    assert outcome.errors == 3
    assert outcome.seen == 3


def test_media_buy_context_uses_keywords():
    buy = MagicMock()
    buy.tenant_id = "t1"
    buy.principal_id = "p1"
    buy.media_buy_id = "mb1"
    ctx = media_buy_context(buy)
    assert ctx.tenant_id == "t1"
    assert ctx.principal_id == "p1"
    assert ctx.media_buy_id == "mb1"


def test_run_isolated_batch_opens_savepoint_when_session_provided():
    session = MagicMock()
    nested = MagicMock()
    session.begin_nested.return_value = nested
    nested.__enter__ = MagicMock(return_value=nested)
    nested.__exit__ = MagicMock(return_value=False)

    calls: list[int] = []

    def handle(item: int) -> bool:
        calls.append(item)
        return True

    outcome = run_isolated_batch(
        [1, 2],
        handle,
        item_context=_ctx,
        on_error=lambda _ctx, _exc: None,
        session=session,
    )

    assert outcome.processed == 2
    assert session.begin_nested.call_count == 2


def test_run_isolated_batch_records_scheduler_metric():
    ctx_obj = media_buy_context(MagicMock(tenant_id="tenant-a", principal_id="p", media_buy_id="mb"))
    raised = OperationalError("SELECT 1", {}, Exception("timeout"))

    def handle(_item: int) -> bool:
        raise raised

    with patch("src.services.isolated_batch.record_scheduler_isolation_error") as mock_record:
        outcome = run_isolated_batch(
            [1],
            handle,
            item_context=lambda _x: ctx_obj,
            on_error=lambda _ctx, _exc: None,
            scheduler="media_buy_status",
        )

    assert outcome.errors == 1
    mock_record.assert_called_once_with(
        scheduler="media_buy_status",
        tenant_id="tenant-a",
        error=raised,
    )

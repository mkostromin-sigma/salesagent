"""Unit tests for scheduler per-item isolation helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import DisconnectionError, OperationalError

from src.services.isolated_batch import (
    BatchOutcome,
    default_escape_isolation,
    media_buy_context,
    run_isolated_batch,
    run_isolated_batch_async,
)


def test_run_isolated_batch_counts_success_and_isolates_errors():
    seen: list[int] = []
    errors: list[tuple[int, str]] = []

    def handle(item: int) -> bool:
        seen.append(item)
        if item == 2:
            raise ValueError("boom")
        return item % 2 == 1

    def on_error(ctx: int, exc: Exception) -> None:
        errors.append((ctx, type(exc).__name__))

    outcome = run_isolated_batch(
        [1, 2, 3],
        handle,
        item_context=lambda x: x,
        on_error=on_error,
    )

    assert seen == [1, 2, 3]
    assert isinstance(outcome, BatchOutcome)
    assert outcome.processed == 2  # 1 and 3 truthy; 2 raised
    assert outcome.errors == 1
    assert errors == [(2, "ValueError")]


def test_run_isolated_batch_isolates_operational_error_without_invalidated():
    """Bare OperationalError (e.g. QueryCanceled) must stay isolatable."""
    seen: list[int] = []

    def handle(item: int) -> bool:
        seen.append(item)
        if item == 1:
            raise OperationalError("SELECT 1", {}, Exception("statement timeout"))
        return True

    outcome = run_isolated_batch(
        [1, 2],
        handle,
        item_context=lambda x: x,
        on_error=lambda _ctx, _exc: None,
    )

    assert seen == [1, 2]
    assert outcome.processed == 1
    assert outcome.errors == 1


def test_run_isolated_batch_reraises_operational_error_when_connection_invalidated():
    def handle(_item: int) -> bool:
        raise OperationalError("SELECT 1", {}, Exception("db down"), connection_invalidated=True)

    with pytest.raises(OperationalError):
        run_isolated_batch(
            [1],
            handle,
            item_context=lambda x: x,
            on_error=lambda _ctx, _exc: None,
        )


def test_run_isolated_batch_reraises_disconnection_error():
    def handle(_item: int) -> bool:
        raise DisconnectionError("gone")

    with pytest.raises(DisconnectionError):
        run_isolated_batch(
            [1],
            handle,
            item_context=lambda x: x,
            on_error=lambda _ctx, _exc: None,
        )


def test_default_escape_isolation_predicate():
    plain = OperationalError("SELECT 1", {}, Exception("timeout"))
    invalidated = OperationalError("SELECT 1", {}, Exception("gone"), connection_invalidated=True)
    assert default_escape_isolation(plain) is False
    assert default_escape_isolation(invalidated) is True
    assert default_escape_isolation(DisconnectionError("gone")) is True
    assert default_escape_isolation(ValueError("x")) is False


def test_raising_on_error_still_visits_all_items():
    seen: list[int] = []

    def handle(item: int) -> bool:
        seen.append(item)
        if item == 1:
            raise ValueError("original")
        return True

    def on_error(_ctx: int, _exc: Exception) -> None:
        raise KeyError("handler blew up")

    outcome = run_isolated_batch(
        [1, 2, 3],
        handle,
        item_context=lambda x: x,
        on_error=on_error,
    )

    assert seen == [1, 2, 3]
    assert outcome.processed == 2
    assert outcome.errors == 1


@pytest.mark.asyncio
async def test_run_isolated_batch_async_isolates_operational_error_without_invalidated():
    async def handle(item: int) -> bool:
        if item == 1:
            raise OperationalError("SELECT 1", {}, Exception("statement timeout"))
        return True

    outcome = await run_isolated_batch_async(
        [1, 2],
        handle,
        item_context=lambda x: x,
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
            item_context=lambda x: x,
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
            item_context=lambda x: x,
            on_error=lambda _ctx, _exc: None,
        )


@pytest.mark.asyncio
async def test_run_isolated_batch_async_mirrors_sync():
    seen: list[int] = []
    errors: list[tuple[int, str]] = []

    async def handle(item: int) -> bool:
        seen.append(item)
        if item == 1:
            raise RuntimeError("x")
        return True

    def on_error(ctx: int, exc: Exception) -> None:
        errors.append((ctx, type(exc).__name__))

    outcome = await run_isolated_batch_async(
        [1, 2],
        handle,
        item_context=lambda x: x,
        on_error=on_error,
    )

    assert seen == [1, 2]
    assert outcome.processed == 1
    assert outcome.errors == 1
    assert errors == [(1, "RuntimeError")]


@pytest.mark.asyncio
async def test_raising_on_error_async_still_visits_all_items():
    seen: list[int] = []

    async def handle(item: int) -> bool:
        seen.append(item)
        if item == 2:
            raise ValueError("original")
        return True

    def on_error(_ctx: int, _exc: Exception) -> None:
        raise RuntimeError("handler blew up")

    outcome = await run_isolated_batch_async(
        [1, 2, 3],
        handle,
        item_context=lambda x: x,
        on_error=on_error,
    )

    assert seen == [1, 2, 3]
    assert outcome.processed == 2
    assert outcome.errors == 1


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
        item_context=lambda x: x,
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

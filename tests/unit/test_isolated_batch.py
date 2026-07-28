"""Unit tests for scheduler per-item isolation helper."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DisconnectionError, OperationalError

from src.services.isolated_batch import run_isolated_batch, run_isolated_batch_async


def test_run_isolated_batch_counts_success_and_isolates_errors():
    seen: list[int] = []
    errors: list[tuple[int, str]] = []

    def handle(item: int) -> bool:
        seen.append(item)
        if item == 2:
            raise ValueError("boom")
        return item % 2 == 1

    def on_error(ctx: int, exc: BaseException) -> None:
        errors.append((ctx, type(exc).__name__))

    processed, error_count = run_isolated_batch(
        [1, 2, 3],
        handle,
        item_context=lambda x: x,
        on_error=on_error,
    )

    assert seen == [1, 2, 3]
    assert processed == 2  # 1 and 3 truthy; 2 raised
    assert error_count == 1
    assert errors == [(2, "ValueError")]


def test_run_isolated_batch_reraises_operational_error():
    def handle(_item: int) -> bool:
        raise OperationalError("SELECT 1", {}, Exception("db down"))

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


@pytest.mark.asyncio
async def test_run_isolated_batch_async_reraises_operational_error():
    async def handle(_item: int) -> bool:
        raise OperationalError("SELECT 1", {}, Exception("db down"))

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
    async def handle(item: int) -> bool:
        if item == 1:
            raise RuntimeError("x")
        return True

    errors: list[int] = []

    processed, error_count = await run_isolated_batch_async(
        [1, 2],
        handle,
        item_context=lambda x: x,
        on_error=lambda ctx, _exc: errors.append(ctx),
    )

    assert processed == 1
    assert error_count == 1
    assert errors == [1]

"""Per-item isolation for scheduler batch loops.

Both production schedulers under ``src/services/`` run a ``get_db_session``
loop that must (a) keep going when one item fails and (b) still let
``OperationalError`` / ``DisconnectionError`` reach ``get_db_session`` so the
process-global circuit breaker can trip.

Callers supply ``item_context`` to capture any values needed for error logging
*before* the item body runs — under production ``autoflush=True`` a flush
failure expires ORM attributes, so reading them inside ``on_error`` can raise
``PendingRollbackError`` and hide the original exception.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence

from src.core.database.database_session import CONNECTION_ERROR_TYPES

#: Exceptions that must not be isolated — re-raise so ``get_db_session`` can
#: trip ``_is_healthy``. SSOT: :data:`CONNECTION_ERROR_TYPES`.
DEFAULT_NON_ISOLATABLE: tuple[type[BaseException], ...] = CONNECTION_ERROR_TYPES


def run_isolated_batch[T, C](
    items: Iterable[T],
    handle_item: Callable[[T], bool | None],
    *,
    item_context: Callable[[T], C],
    on_error: Callable[[C, BaseException], None],
    non_isolatable: Sequence[type[BaseException]] = DEFAULT_NON_ISOLATABLE,
) -> tuple[int, int]:
    """Run ``handle_item`` per item with isolation.

    Returns ``(processed_count, error_count)``. ``handle_item`` should return a
    truthy value when the item counts toward ``processed_count`` (e.g. a status
    flip or a report sent). ``False`` / ``None`` means "handled, no tally".
    """
    processed = 0
    errors = 0
    non_isolatable_types = tuple(non_isolatable)

    for item in items:
        ctx = item_context(item)
        try:
            if handle_item(item):
                processed += 1
        except non_isolatable_types:
            raise
        except Exception as exc:
            errors += 1
            on_error(ctx, exc)

    return processed, errors


async def run_isolated_batch_async[T, C](
    items: Iterable[T],
    handle_item: Callable[[T], Awaitable[bool | None]],
    *,
    item_context: Callable[[T], C],
    on_error: Callable[[C, BaseException], None],
    non_isolatable: Sequence[type[BaseException]] = DEFAULT_NON_ISOLATABLE,
) -> tuple[int, int]:
    """Async variant of :func:`run_isolated_batch`."""
    processed = 0
    errors = 0
    non_isolatable_types = tuple(non_isolatable)

    for item in items:
        ctx = item_context(item)
        try:
            if await handle_item(item):
                processed += 1
        except non_isolatable_types:
            raise
        except Exception as exc:
            errors += 1
            on_error(ctx, exc)

    return processed, errors

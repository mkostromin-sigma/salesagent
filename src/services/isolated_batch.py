"""Per-item isolation for scheduler batch loops.

Both production schedulers under ``src/services/`` run a ``get_db_session``
loop that must (a) keep going when one item fails and (b) still let a *dead*
connection escape so ``get_db_session`` can trip the process-global circuit
breaker.

Escape is gated on connection state
(``connection_invalidated`` / ``DisconnectionError``), not on broad
``OperationalError`` membership — statement timeouts and similar per-row DB
failures inherit ``OperationalError`` but leave the connection usable after
``ROLLBACK TO SAVEPOINT``.

Callers supply ``item_context`` to capture any values needed for error logging
*before* the item body runs — under production ``autoflush=True`` a flush
failure expires ORM attributes, so reading them inside ``on_error`` can raise
``PendingRollbackError`` and hide the original exception. Capture happens
outside the per-item ``try``; ``item_context`` must not touch deferred columns
or relationships that need a live flush.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import contextmanager, nullcontext
from typing import NamedTuple, Protocol

from sqlalchemy.exc import DisconnectionError
from sqlalchemy.orm import Session

from src.core.metrics import record_scheduler_isolation_error

logger = logging.getLogger(__name__)


class BatchOutcome(NamedTuple):
    """Tallied result of an isolated batch run."""

    processed: int
    errors: int


class SchedulerItemContext(NamedTuple):
    """Ids captured before the item body for safe error logging/metrics."""

    tenant_id: str
    principal_id: str
    media_buy_id: str


class _HasMediaBuyIds(Protocol):
    tenant_id: str
    principal_id: str
    media_buy_id: str


def media_buy_context(media_buy: _HasMediaBuyIds) -> SchedulerItemContext:
    """Build :class:`SchedulerItemContext` with keyword fields (swap-safe)."""
    return SchedulerItemContext(
        tenant_id=media_buy.tenant_id,
        principal_id=media_buy.principal_id,
        media_buy_id=media_buy.media_buy_id,
    )


def default_escape_isolation(exc: Exception) -> bool:
    """Return True when ``exc`` must escape isolation (dead connection)."""
    return bool(getattr(exc, "connection_invalidated", False)) or isinstance(exc, DisconnectionError)


def log_batch_summary(
    batch_logger: logging.Logger,
    prefix: str,
    processed: int,
    errors: int,
    *,
    suppress_when_quiet: bool = False,
    success_label: str = "processed",
) -> None:
    """Emit one batch summary line; WARNING when every item failed.

    When ``suppress_when_quiet`` is True, a fully quiet tick (0 processed,
    0 errors) is skipped — appropriate for high-cadence schedulers.
    """
    if suppress_when_quiet and not processed and not errors:
        return
    summary = f"{prefix}: {processed} {success_label}, {errors} errors"
    if errors and not processed:
        batch_logger.warning(summary)
    else:
        batch_logger.info(summary)


@contextmanager
def _item_transaction_scope(session: Session | None):
    """Open a SAVEPOINT when a session is provided; otherwise no-op."""
    if session is None:
        with nullcontext():
            yield
    else:
        with session.begin_nested():
            yield


def _safe_on_error[C](on_error: Callable[[C, Exception], None], ctx: C, exc: Exception) -> None:
    """Invoke ``on_error`` without letting a raising handler abort the batch."""
    try:
        on_error(ctx, exc)
    except Exception:
        logger.exception("on_error handler failed while handling %s", type(exc).__name__)


def _tally_isolated_failure[C](
    *,
    ctx: C,
    exc: Exception,
    on_error: Callable[[C, Exception], None],
    scheduler: str | None,
) -> None:
    """Shared escape-miss path: guard on_error and optionally meter."""
    _safe_on_error(on_error, ctx, exc)
    if scheduler is not None:
        tenant_id = getattr(ctx, "tenant_id", None)
        if tenant_id is not None:
            record_scheduler_isolation_error(scheduler=scheduler, tenant_id=str(tenant_id), error=exc)


def run_isolated_batch[T, C](
    items: Iterable[T],
    handle_item: Callable[[T], bool | None],
    *,
    item_context: Callable[[T], C],
    on_error: Callable[[C, Exception], None],
    escape_isolation: Callable[[Exception], bool] = default_escape_isolation,
    session: Session | None = None,
    scheduler: str | None = None,
) -> BatchOutcome:
    """Run ``handle_item`` per item with isolation.

    Returns :class:`BatchOutcome`. ``handle_item`` should return a truthy value
    when the item counts toward ``processed`` (e.g. a status flip or a report
    sent). ``False`` / ``None`` means "handled, no tally".

    When ``session`` is provided, each item runs inside ``session.begin_nested()``
    so a DB error rolls back only that item. When ``scheduler`` is set, isolated
    failures are recorded on ``scheduler_isolation_errors_total``.
    """
    processed = 0
    errors = 0

    for item in items:
        ctx = item_context(item)
        try:
            with _item_transaction_scope(session):
                if handle_item(item):
                    processed += 1
        except Exception as exc:
            if escape_isolation(exc):
                raise
            errors += 1
            _tally_isolated_failure(ctx=ctx, exc=exc, on_error=on_error, scheduler=scheduler)

    return BatchOutcome(processed=processed, errors=errors)


async def run_isolated_batch_async[T, C](
    items: Iterable[T],
    handle_item: Callable[[T], Awaitable[bool | None]],
    *,
    item_context: Callable[[T], C],
    on_error: Callable[[C, Exception], None],
    escape_isolation: Callable[[Exception], bool] = default_escape_isolation,
    session: Session | None = None,
    scheduler: str | None = None,
) -> BatchOutcome:
    """Async variant of :func:`run_isolated_batch`."""
    processed = 0
    errors = 0

    for item in items:
        ctx = item_context(item)
        try:
            with _item_transaction_scope(session):
                if await handle_item(item):
                    processed += 1
        except Exception as exc:
            if escape_isolation(exc):
                raise
            errors += 1
            _tally_isolated_failure(ctx=ctx, exc=exc, on_error=on_error, scheduler=scheduler)

    return BatchOutcome(processed=processed, errors=errors)

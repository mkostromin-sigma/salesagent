"""Per-item isolation for scheduler batch loops.

Both production schedulers under ``src/services/`` run a ``get_db_session``
loop that must (a) keep going when one item fails and (b) still let a *dead*
connection escape so ``get_db_session`` can trip the process-global circuit
breaker.

Escape is gated on connection *state*
(``connection_invalidated`` / ``DisconnectionError``), not on broad
``OperationalError`` membership — statement timeouts and similar per-row DB
failures inherit ``OperationalError`` but leave the connection usable after
``ROLLBACK TO SAVEPOINT``. The breaker's class tuple
(``CONNECTION_ERROR_TYPES`` in ``database_session``) is a related but
distinct set: it must include every exception class that escape can raise so
an escaped dead connection actually arms the breaker.

Optional ``session=`` opens ``begin_nested()`` per item for ORM-mutating
callers (status scheduler). Delivery keeps ``session=None`` because
``get_db_session`` is thread-scoped: the webhook path opens a nested
``with get_db_session()`` that returns the *same* Session and closes it on
exit, which a caller-side SAVEPOINT cannot survive. Consequently the
SAVEPOINT safety property above (statement timeouts leave the connection
usable after ``ROLLBACK TO SAVEPOINT``) does **not** apply to delivery —
delivery isolates non-ORM send failures and meters them, and relies on
``session.rollback()`` in ``on_error`` to clear a poisoned shared
transaction when a pre-send DB read fails.

Callers supply ``item_context`` to capture any values needed for error logging
*before* the item body runs — under production ``autoflush=True`` a flush
failure expires ORM attributes, so reading them inside ``on_error`` can raise
``PendingRollbackError`` and hide the original exception. Capture is attempted
inside the per-item isolation scope so a raising ``item_context`` is metered
rather than aborting the batch; ``item_context`` must not touch deferred
columns or relationships that need a live flush.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import contextmanager
from types import SimpleNamespace
from typing import NamedTuple, Protocol

from sqlalchemy.exc import DisconnectionError
from sqlalchemy.orm import Session

from src.core.metrics import record_scheduler_isolation_error

logger = logging.getLogger(__name__)


class BatchOutcome(NamedTuple):
    """Tallied result of an isolated batch run.

    ``seen`` is the number of items visited. ``processed`` counts only items
    whose handler returned truthy *and* whose transaction scope (if any)
    released cleanly. ``errors`` counts isolated failures.
    """

    processed: int
    errors: int
    seen: int


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
    seen: int | None = None,
    suppress_when_quiet: bool = False,
    success_label: str = "processed",
) -> None:
    """Emit one batch summary line; WARNING when every visited item failed.

    When ``suppress_when_quiet`` is True, a fully quiet tick (0 processed,
    0 errors) is skipped — appropriate for high-cadence schedulers.

    ``seen`` is the item count visited by the runner. When provided, WARNING
    fires only if ``errors == seen and seen > 0`` (every item failed). Falsy
    handler returns (legitimate no-ops) therefore do not escalate a single
    failure into an all-fail WARNING. When ``seen`` is omitted, the legacy
    ``errors and not processed`` rule is used.
    """
    if suppress_when_quiet and not processed and not errors:
        return
    summary = f"{prefix}: {processed} {success_label}, {errors} errors"
    if seen is not None:
        every_item_failed = bool(seen) and errors == seen
    else:
        every_item_failed = bool(errors) and not processed
    if every_item_failed:
        batch_logger.warning(summary)
    else:
        batch_logger.info(summary)


def log_item_error(
    batch_logger: logging.Logger,
    prefix: str,
    ctx: SchedulerItemContext,
    exc: Exception,
) -> None:
    """Emit one per-item error line with swap-safe context fields."""
    batch_logger.error(
        f"{prefix} "
        f"(tenant_id={ctx.tenant_id}, principal_id={ctx.principal_id}, "
        f"media_buy_id={ctx.media_buy_id}): {exc}",
        exc_info=True,
    )


@contextmanager
def _item_transaction_scope(session: Session | None):
    """Open a SAVEPOINT when a session is provided; otherwise no-op."""
    if session is None:
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


def _should_escape(escape_isolation: Callable[[Exception], bool], exc: Exception) -> bool:
    """Evaluate the escape predicate; a raising predicate does not escape.

    A broken predicate must not abort the remaining items or demote the
    original exception to ``__context__`` — same contract as ``on_error``.
    """
    try:
        return bool(escape_isolation(exc))
    except Exception:
        logger.exception(
            "escape_isolation predicate failed while handling %s; treating as isolated",
            type(exc).__name__,
        )
        return False


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
        # Callers that pass ``scheduler=`` must supply a context with tenant_id
        # (both production sites use :class:`SchedulerItemContext`).
        record_scheduler_isolation_error(
            scheduler=scheduler,
            tenant_id=ctx.tenant_id,  # type: ignore[attr-defined]
            error=exc,
        )


@contextmanager
def _catch_isolated(escape_isolation: Callable[[Exception], bool]):
    """Swallow isolatable errors into ``box.error``; re-raise escapes.

    Sync and async runners share this policy so the escape decision lives in
    one place (a plain ``with`` is legal around ``await`` inside ``async def``).
    """
    box = SimpleNamespace(error=None)
    try:
        yield box
    except Exception as exc:
        if _should_escape(escape_isolation, exc):
            raise
        box.error = exc


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
    so a DB error rolls back only that item. The processed tally is applied
    *after* the savepoint releases successfully — a release-time flush failure
    counts as an error, not a success. When ``scheduler`` is set, isolated
    failures are recorded on ``scheduler_isolation_errors_total``.
    """
    processed = 0
    errors = 0
    seen = 0

    for item in items:
        seen += 1
        ctx: C | None = None
        with _catch_isolated(escape_isolation) as box:
            ctx = item_context(item)
            with _item_transaction_scope(session):
                tallied = bool(handle_item(item))
            # Only reached when the savepoint (if any) released cleanly.
            if tallied:
                processed += 1
        if box.error is not None:
            errors += 1
            if ctx is None:
                logger.exception(
                    "item_context failed before handle_item; isolating without typed ctx (%s)",
                    type(box.error).__name__,
                )
            else:
                _tally_isolated_failure(ctx=ctx, exc=box.error, on_error=on_error, scheduler=scheduler)

    return BatchOutcome(processed=processed, errors=errors, seen=seen)


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
    seen = 0

    for item in items:
        seen += 1
        ctx: C | None = None
        with _catch_isolated(escape_isolation) as box:
            ctx = item_context(item)
            with _item_transaction_scope(session):
                tallied = bool(await handle_item(item))
            if tallied:
                processed += 1
        if box.error is not None:
            errors += 1
            if ctx is None:
                logger.exception(
                    "item_context failed before handle_item; isolating without typed ctx (%s)",
                    type(box.error).__name__,
                )
            else:
                _tally_isolated_failure(ctx=ctx, exc=box.error, on_error=on_error, scheduler=scheduler)

    return BatchOutcome(processed=processed, errors=errors, seen=seen)

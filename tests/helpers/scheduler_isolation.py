"""Shared helpers for scheduler isolation test oracles."""

from __future__ import annotations

from unittest.mock import MagicMock


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

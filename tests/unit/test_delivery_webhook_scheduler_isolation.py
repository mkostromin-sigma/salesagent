"""Pure helper/adoption stubs for delivery isolation.

Real per-buy isolation against live PostgreSQL + webhook send is graded by
``tests/integration/test_delivery_webhook_scheduler_isolation.py``. This module
only keeps cheap unit coverage that does not reimplement the production
``get_db_session`` breaker arm.
"""

from __future__ import annotations

from src.core.database.database_session import is_connection_dead
from src.services.delivery_webhook_scheduler import DELIVERY_BATCH_SUMMARY_PREFIX
from src.services.isolated_batch import default_escape_isolation, log_batch_summary
from tests.helpers.scheduler_isolation import summary_lines


def test_delivery_summary_prefix_constant_matches_log_batch_summary():
    """Prefix constant is what production emits — helpers must use the same."""
    from unittest.mock import MagicMock

    mock_logger = MagicMock()
    log_batch_summary(mock_logger, DELIVERY_BATCH_SUMMARY_PREFIX, 2, 1, seen=3, success_label="sent")
    lines = summary_lines(mock_logger.info, DELIVERY_BATCH_SUMMARY_PREFIX)
    assert lines == [f"{DELIVERY_BATCH_SUMMARY_PREFIX}: 2 sent, 1 errors"]


def test_delivery_escape_predicate_is_db_layer_owned():
    """Delivery escape must share the DB-layer dead-connection predicate."""
    assert default_escape_isolation is is_connection_dead

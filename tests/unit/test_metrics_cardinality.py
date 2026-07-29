"""Regression tests for Prometheus label cardinality bounds.

These tests guard against the OOM contributor identified 2026-05-18: per-tenant
Prometheus series accumulating without bound, plus unbounded ``error_type`` (from
``type(e).__name__``) and free-form ``policy_triggered`` labels.


"""

import pytest
from prometheus_client import Counter, Histogram
from sqlalchemy.exc import OperationalError


def _series_count(collector) -> int:
    """Number of distinct label series exposed by a Counter/Gauge collector."""
    return len(list(collector.collect())[0].samples)


def test_no_histogram_has_tenant_id_label():
    """Histograms allocate a bucket array per series — tenant_id makes them
    grow linearly with tenant count. No Histogram may carry tenant_id."""
    from src.core import metrics

    offenders = []
    for name in dir(metrics):
        obj = getattr(metrics, name)
        if isinstance(obj, Histogram):
            label_names = obj._labelnames
            if "tenant_id" in label_names:
                offenders.append((name, label_names))

    assert not offenders, f"Histograms must not label by tenant_id: {offenders}"


def test_categorize_error_bounds_error_type_to_enum():
    """categorize_error must collapse arbitrary exceptions into a fixed enum."""
    from src.core.metrics import ERROR_TYPE_VALUES, categorize_error

    allowed = set(ERROR_TYPE_VALUES)

    # 1000 distinct exception classes must all map into the fixed enum.
    seen = set()
    for i in range(1000):
        exc_cls = type(f"FakeError{i}", (Exception,), {})
        seen.add(categorize_error(exc_cls("boom")))

    assert seen <= allowed, f"categorize_error produced out-of-enum values: {seen - allowed}"
    assert len(allowed) <= 5


def _error_type_counters():
    """Derive Counters that carry a bounded ``error_type`` label."""
    from src.core import metrics

    found = []
    for name in dir(metrics):
        obj = getattr(metrics, name)
        if isinstance(obj, Counter) and "error_type" in getattr(obj, "_labelnames", ()):
            found.append((name, obj))
    return found


@pytest.mark.parametrize(
    ("name", "recorder_kwargs"),
    [
        ("ai_review_errors", {"via": "ai_review"}),
        ("scheduler_isolation_errors", {"via": "scheduler", "scheduler": "media_buy_status"}),
        ("scheduler_isolation_errors", {"via": "scheduler", "scheduler": "delivery_webhook"}),
    ],
)
def test_error_type_counter_cardinality_bounded(name, recorder_kwargs):
    """Recording 1000 unique error types for one tenant must stay bounded."""
    from src.core import metrics

    assert name in {n for n, _ in _error_type_counters()}
    collector = getattr(metrics, name)
    collector.clear()

    if recorder_kwargs["via"] == "ai_review":
        for i in range(1000):
            exc_cls = type(f"FakeError{i}", (Exception,), {})
            metrics.record_ai_review_error(tenant_id="t1", error=exc_cls("boom"))
    else:
        for i in range(1000):
            exc_cls = type(f"FakeSchedError{i}", (Exception,), {})
            metrics.record_scheduler_isolation_error(
                scheduler=recorder_kwargs["scheduler"],
                tenant_id="t1",
                error=exc_cls("boom"),
            )

    # prometheus emits _total + _created per label set.
    assert _series_count(collector) <= len(metrics.ERROR_TYPE_VALUES) * 2


def test_scheduler_isolation_oracle_uses_db_error_class():
    """Unit oracle must exercise a class the scheduler loop can raise."""
    from src.core import metrics

    metrics.scheduler_isolation_errors.clear()
    metrics.record_scheduler_isolation_error(
        scheduler="media_buy_status",
        tenant_id="t1",
        error=OperationalError("SELECT 1", {}, Exception("timeout")),
    )
    assert (
        metrics.scheduler_isolation_errors.labels(
            scheduler="media_buy_status", tenant_id="t1", error_type="db_error"
        )._value.get()
        == 1
    )


def test_sanitize_policy_triggered_allowlist():
    """Unknown / AI-driven free-form policy_triggered values collapse to 'other'."""
    from src.core.metrics import POLICY_TRIGGERED_ALLOWLIST, sanitize_policy_triggered

    # Known values pass through unchanged.
    for known in POLICY_TRIGGERED_ALLOWLIST:
        assert sanitize_policy_triggered(known) == known

    # Arbitrary AI-generated strings collapse to a single bucket.
    for i in range(1000):
        assert sanitize_policy_triggered(f"ai_made_up_reason_{i}") == "other"

    assert sanitize_policy_triggered(None) == "other"


def test_sanitize_scheduler_allowlist():
    from src.core.metrics import SCHEDULER_ALLOWLIST, sanitize_scheduler

    for known in SCHEDULER_ALLOWLIST:
        assert sanitize_scheduler(known) == known
    assert sanitize_scheduler("not_a_real_scheduler") == "other"
    assert sanitize_scheduler(None) == "other"


def test_ai_review_total_cardinality_bounded_under_freeform_policy():
    """Feeding 1000 free-form policy_triggered values through the recording
    path must not explode ai_review_total series for a single tenant/decision."""
    from src.core import metrics

    metrics.ai_review_total.clear()
    for i in range(1000):
        metrics.record_ai_review(
            tenant_id="t1",
            decision="pending_review",
            policy_triggered=f"free_form_{i}",
        )

    # tenant t1 x decision pending_review x policy in {whatever known + other}.
    # Free-form all collapse to 'other' -> 1 label set -> <= 2 samples.
    assert _series_count(metrics.ai_review_total) <= 4

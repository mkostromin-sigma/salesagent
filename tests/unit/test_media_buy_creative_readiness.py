"""Unit tests for shared creative finalize-readiness predicate (#1696)."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

from src.core.schemas.creative import FINALIZE_READY_CREATIVE_STATUSES, CreativeStatusEnum
from src.services.media_buy_creative_readiness import (
    CreativeFinalizeReadiness,
    _coerce_flight_boundary,
    apply_creative_finalize_hold,
    apply_creative_finalize_ready,
    compute_media_buy_status_from_flight_dates,
    evaluate_creative_finalize_readiness,
    evaluate_creative_finalize_readiness_for_session,
    log_creative_finalize_hold,
    stamp_media_buy_approval,
)


def _assignment(creative_id: str, principal_id: str = "p1") -> MagicMock:
    a = MagicMock()
    a.creative_id = creative_id
    a.principal_id = principal_id
    return a


def _creative(creative_id: str, status: str) -> MagicMock:
    c = MagicMock()
    c.creative_id = creative_id
    c.status = status
    return c


def _repos(assignments: list, creatives_by_call: list[list] | None = None):
    """Mock assignment + creative repositories for the shared predicate."""
    assignments_repo = MagicMock()
    creatives_repo = MagicMock()
    assignments_repo.get_by_media_buy.return_value = assignments
    if creatives_by_call is None:
        creatives_repo.get_by_ids.return_value = []
    elif len(creatives_by_call) == 1:
        creatives_repo.get_by_ids.return_value = creatives_by_call[0]
    else:
        creatives_repo.get_by_ids.side_effect = creatives_by_call
    return assignments_repo, creatives_repo


class TestEvaluateCreativeFinalizeReadiness:
    def test_zero_assignments_not_ready_no_assignments(self):
        assignments_repo, creatives_repo = _repos([])
        result = evaluate_creative_finalize_readiness(assignments_repo, creatives_repo, media_buy_id="mb_1")
        assert result.ready is False
        assert result.unapproved_creative_ids == []
        assert result.hold_reason == "no_assignments"
        assert result.hold_message is not None
        assert "assigned" in result.hold_message
        creatives_repo.get_by_ids.assert_not_called()

    def test_all_approved_ready(self):
        assignments_repo, creatives_repo = _repos(
            [_assignment("c1"), _assignment("c2")],
            [[_creative("c1", "approved"), _creative("c2", "approved")]],
        )
        result = evaluate_creative_finalize_readiness(assignments_repo, creatives_repo, media_buy_id="mb_1")
        assert result.ready is True
        assert result.unapproved_creative_ids == []
        assert result.hold_reason is None
        assert result.hold_message is None
        creatives_repo.get_by_ids.assert_called_once_with(["c1", "c2"], "p1")

    def test_active_status_counts_as_ready(self):
        """Legacy ``active`` remains in the shared allowlist; pin against enum + sole legacy."""
        enum_values = {m.value for m in CreativeStatusEnum}
        assert FINALIZE_READY_CREATIVE_STATUSES - {"active"} <= enum_values
        assert FINALIZE_READY_CREATIVE_STATUSES - enum_values == {"active"}
        assignments_repo, creatives_repo = _repos(
            [_assignment("c1")],
            [[_creative("c1", "active")]],
        )
        result = evaluate_creative_finalize_readiness(assignments_repo, creatives_repo, media_buy_id="mb_1")
        assert result.ready is True
        assert result.hold_reason is None

    def test_pending_creative_not_ready(self):
        assignments_repo, creatives_repo = _repos(
            [_assignment("c1"), _assignment("c2")],
            [[_creative("c1", "approved"), _creative("c2", "pending_review")]],
        )
        result = evaluate_creative_finalize_readiness(assignments_repo, creatives_repo, media_buy_id="mb_1")
        assert result.ready is False
        assert result.unapproved_creative_ids == ["c2"]
        assert result.hold_reason == "unapproved_creatives"
        assert "1 creative" in (result.hold_message or "")

    def test_rejected_creative_not_ready(self):
        assignments_repo, creatives_repo = _repos(
            [_assignment("c1")],
            [[_creative("c1", "rejected")]],
        )
        result = evaluate_creative_finalize_readiness(assignments_repo, creatives_repo, media_buy_id="mb_1")
        assert result.ready is False
        assert result.unapproved_creative_ids == ["c1"]
        assert result.hold_reason == "unapproved_creatives"

    def test_missing_creative_row_counts_as_unapproved(self):
        assignments_repo, creatives_repo = _repos(
            [_assignment("c1"), _assignment("c_missing")],
            [[_creative("c1", "approved")]],
        )
        result = evaluate_creative_finalize_readiness(assignments_repo, creatives_repo, media_buy_id="mb_1")
        assert result.ready is False
        assert result.hold_reason == "unapproved_creatives"
        assert "c_missing" in result.unapproved_creative_ids

    def test_loads_creatives_per_principal(self):
        assignments_repo, creatives_repo = _repos(
            [_assignment("c1", "p1"), _assignment("c2", "p2")],
            [[_creative("c1", "approved")], [_creative("c2", "approved")]],
        )
        result = evaluate_creative_finalize_readiness(assignments_repo, creatives_repo, media_buy_id="mb_x")
        assert result.ready is True
        assert creatives_repo.get_by_ids.call_count == 2
        creatives_repo.get_by_ids.assert_any_call(["c1"], "p1")
        creatives_repo.get_by_ids.assert_any_call(["c2"], "p2")
        assignments_repo.get_by_media_buy.assert_called_once_with("mb_x")


class TestEvaluateCreativeFinalizeReadinessForSession:
    def test_builds_repos_and_delegates(self):
        session = MagicMock()
        readiness = CreativeFinalizeReadiness(
            ready=False,
            unapproved_creative_ids=[],
            hold_reason="no_assignments",
            hold_message="held",
        )
        with (
            patch(
                "src.services.media_buy_creative_readiness.CreativeAssignmentRepository",
            ) as mock_assign_cls,
            patch(
                "src.services.media_buy_creative_readiness.CreativeRepository",
            ) as mock_creative_cls,
            patch(
                "src.services.media_buy_creative_readiness.evaluate_creative_finalize_readiness",
                return_value=readiness,
            ) as mock_eval,
        ):
            result = evaluate_creative_finalize_readiness_for_session(session, "tenant_1", media_buy_id="mb_1")

        assert result is readiness
        mock_assign_cls.assert_called_once_with(session, "tenant_1")
        mock_creative_cls.assert_called_once_with(session, "tenant_1")
        mock_eval.assert_called_once_with(
            mock_assign_cls.return_value,
            mock_creative_cls.return_value,
            media_buy_id="mb_1",
        )


class TestApplyCreativeFinalizeHold:
    def test_sets_status_provenance_and_logs_hold_reason(self, caplog):
        media_buy = MagicMock()
        media_buy.media_buy_id = "mb_hold"
        readiness = CreativeFinalizeReadiness(
            ready=False,
            unapproved_creative_ids=["c1"],
            hold_reason="unapproved_creatives",
            hold_message="waiting",
        )
        with caplog.at_level("INFO", logger="src.services.media_buy_creative_readiness"):
            apply_creative_finalize_hold(media_buy, readiness, approved_by="op@example.com")

        assert media_buy.status == "pending_creatives"
        assert media_buy.approved_by == "op@example.com"
        assert isinstance(media_buy.approved_at, datetime)
        assert any("hold_reason=unapproved_creatives" in r.message for r in caplog.records)
        assert any("[APPROVAL]" in r.message for r in caplog.records)
        assert any("event=creative_finalize_hold" in r.message for r in caplog.records)
        assert not any(" reason=" in r.message for r in caplog.records)


class TestApplyCreativeFinalizeReady:
    def test_stamps_provenance_and_flight_status(self):
        media_buy = MagicMock()
        media_buy.start_time = datetime.now(UTC) + timedelta(days=7)
        media_buy.end_time = datetime.now(UTC) + timedelta(days=37)
        media_buy.start_date = None
        media_buy.end_date = None
        apply_creative_finalize_ready(media_buy, approved_by="op@example.com")
        assert media_buy.approved_by == "op@example.com"
        assert isinstance(media_buy.approved_at, datetime)
        assert media_buy.status == "scheduled"


class TestStampMediaBuyApproval:
    def test_stamps_provenance(self):
        media_buy = MagicMock()
        stamp_media_buy_approval(media_buy, approved_by="op@example.com")
        assert media_buy.approved_by == "op@example.com"
        assert isinstance(media_buy.approved_at, datetime)


class TestLogCreativeFinalizeHold:
    def test_uses_hold_reason_key(self, caplog):
        readiness = CreativeFinalizeReadiness(
            ready=False,
            unapproved_creative_ids=[],
            hold_reason="no_assignments",
            hold_message="held",
        )
        with caplog.at_level("INFO", logger="src.services.media_buy_creative_readiness"):
            log_creative_finalize_hold("mb_x", readiness, context_tag="[CREATIVE APPROVAL]")
        assert any("hold_reason=no_assignments" in r.message for r in caplog.records)
        assert any("[CREATIVE APPROVAL]" in r.message for r in caplog.records)
        assert any("event=creative_finalize_hold" in r.message for r in caplog.records)


class TestCoerceFlightBoundary:
    def test_aware_datetime_passthrough(self):
        dt = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert _coerce_flight_boundary(dt, None, end_of_day=False) == dt

    def test_naive_datetime_assumes_utc(self):
        dt = datetime(2026, 1, 1, 12, 0)
        result = _coerce_flight_boundary(dt, None, end_of_day=False)
        assert result == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def test_date_start_of_day(self):
        result = _coerce_flight_boundary(None, date(2026, 3, 1), end_of_day=False)
        assert result == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)

    def test_date_end_of_day(self):
        result = _coerce_flight_boundary(None, date(2026, 3, 1), end_of_day=True)
        assert result is not None
        assert result.date() == date(2026, 3, 1)
        assert result.hour == 23

    def test_none_when_both_missing(self):
        assert _coerce_flight_boundary(None, None, end_of_day=False) is None


class TestComputeMediaBuyStatusFromFlightDates:
    def test_completed_when_past_end(self):
        mb = MagicMock()
        mb.start_time = datetime.now(UTC) - timedelta(days=10)
        mb.end_time = datetime.now(UTC) - timedelta(days=1)
        mb.start_date = None
        mb.end_date = None
        assert compute_media_buy_status_from_flight_dates(mb) == "completed"

    def test_scheduled_when_before_start(self):
        mb = MagicMock()
        mb.start_time = datetime.now(UTC) + timedelta(days=2)
        mb.end_time = datetime.now(UTC) + timedelta(days=10)
        mb.start_date = None
        mb.end_date = None
        assert compute_media_buy_status_from_flight_dates(mb) == "scheduled"

    def test_date_columns_via_coerce(self):
        mb = MagicMock()
        mb.start_time = None
        mb.end_time = None
        mb.start_date = (datetime.now(UTC) + timedelta(days=2)).date()
        mb.end_date = (datetime.now(UTC) + timedelta(days=10)).date()
        assert compute_media_buy_status_from_flight_dates(mb) == "scheduled"

    def test_active_when_in_window(self):
        mb = MagicMock()
        mb.start_time = datetime.now(UTC) - timedelta(days=1)
        mb.end_time = datetime.now(UTC) + timedelta(days=10)
        mb.start_date = None
        mb.end_date = None
        assert compute_media_buy_status_from_flight_dates(mb) == "active"

    def test_active_when_only_start_in_past(self):
        """Single-boundary fallback: past start_time alone → active."""
        mb = MagicMock()
        mb.start_time = datetime.now(UTC) - timedelta(days=1)
        mb.end_time = None
        mb.start_date = None
        mb.end_date = None
        assert compute_media_buy_status_from_flight_dates(mb) == "active"

    def test_scheduled_when_only_start_in_future(self):
        """Single-boundary: future start_time alone → scheduled."""
        mb = MagicMock()
        mb.start_time = datetime.now(UTC) + timedelta(days=2)
        mb.end_time = None
        mb.start_date = None
        mb.end_date = None
        assert compute_media_buy_status_from_flight_dates(mb) == "scheduled"

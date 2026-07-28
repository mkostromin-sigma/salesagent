"""Unit tests for shared creative finalize-readiness predicate (#1696)."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.core.schemas.creative import FINALIZE_READY_CREATIVE_STATUSES, CreativeStatusEnum
from src.services.media_buy_creative_readiness import (
    CreativeFinalizeReadiness,
    _coerce_flight_boundary,
    apply_creative_finalize_hold,
    compute_media_buy_status_from_flight_dates,
    evaluate_creative_finalize_readiness,
    evaluate_creative_finalize_readiness_for_session,
    log_creative_finalize_hold,
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
        assert media_buy.approved_at is not None
        assert any("hold_reason=unapproved_creatives" in r.message for r in caplog.records)
        assert not any(" reason=" in r.message for r in caplog.records)


class TestLogCreativeFinalizeHold:
    def test_uses_hold_reason_key(self, caplog):
        readiness = CreativeFinalizeReadiness(
            ready=False,
            unapproved_creative_ids=[],
            hold_reason="no_assignments",
            hold_message="held",
        )
        with caplog.at_level("INFO", logger="src.services.media_buy_creative_readiness"):
            log_creative_finalize_hold("mb_x", readiness)
        assert any("hold_reason=no_assignments" in r.message for r in caplog.records)


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


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.mark.parametrize(
    "hold",
    [
        CreativeFinalizeReadiness(
            ready=False,
            unapproved_creative_ids=[],
            hold_reason="no_assignments",
            hold_message="Media buy approved! Waiting for creatives to be assigned and approved before creating in GAM.",
        ),
        CreativeFinalizeReadiness(
            ready=False,
            unapproved_creative_ids=["c_pending"],
            hold_reason="unapproved_creatives",
            hold_message="Media buy approved! Waiting for 1 creative(s) to be approved before creating in GAM.",
        ),
    ],
)
class TestApproveRoutesHoldBehavior:
    """Hold arm: pending_creatives + execute_approved_media_buy must not run."""

    def test_approve_workflow_step_holds_without_execute(self, hold):
        from src.admin.app import create_app
        from src.admin.blueprints import workflows

        app = create_app()
        media_buy = MagicMock()
        media_buy.media_buy_id = "mb_hold"
        media_buy.status = "pending_approval"

        step = MagicMock()
        mapping = MagicMock()
        mapping.object_type = "media_buy"
        mapping.object_id = "mb_hold"

        db = MagicMock()
        db_cm = MagicMock()
        db_cm.__enter__ = MagicMock(return_value=db)
        db_cm.__exit__ = MagicMock(return_value=False)

        approve = _unwrap(workflows.approve_workflow_step)
        with (
            app.test_request_context(
                "/tenant/t1/workflows/wf1/steps/s1/approve",
                method="POST",
            ),
            patch("src.admin.blueprints.workflows.get_db_session", return_value=db_cm),
            patch("src.admin.blueprints.workflows.WorkflowRepository") as mock_wf_repo_cls,
            patch("src.admin.blueprints.workflows.MediaBuyRepository") as mock_mb_repo_cls,
            patch(
                "src.services.media_buy_creative_readiness.evaluate_creative_finalize_readiness_for_session",
                return_value=hold,
            ) as mock_eval,
            patch(
                "src.core.tools.media_buy_create.execute_approved_media_buy",
            ) as mock_execute,
            patch("src.admin.services.media_buy_creative_readiness.flash") as mock_flash,
            patch("src.admin.blueprints.workflows.session", {"user": {"email": "op@example.com"}}),
        ):
            wf_repo = mock_wf_repo_cls.return_value
            wf_repo.update_status.return_value = step
            wf_repo.get_mappings_for_step.return_value = [mapping]
            mock_mb_repo_cls.return_value.get_by_id.return_value = media_buy

            response, status = approve("t1", "wf1", "s1")

        assert status == 200
        assert response.get_json()["success"] is True
        assert media_buy.status == "pending_creatives"
        assert media_buy.approved_by == "op@example.com"
        mock_eval.assert_called_once_with(db, "t1", media_buy_id="mb_hold")
        mock_execute.assert_not_called()
        # Step-status commit, then hold pending_creatives commit via apply helper.
        assert db.commit.call_count == 2
        mock_flash.assert_called_once_with(hold.hold_message, "info")

    def test_approve_media_buy_holds_without_execute(self, hold):
        from src.admin.app import create_app
        from src.admin.blueprints import operations

        app = create_app()
        media_buy = MagicMock()
        media_buy.media_buy_id = "mb_hold"
        media_buy.status = "pending_approval"
        media_buy.start_time = None
        media_buy.end_time = None
        media_buy.principal_id = "p1"

        step = MagicMock()
        step.step_id = "step_1"
        step.context_id = "ctx_1"
        step.tool_name = "create_media_buy"
        step.request_data = {}
        step.comments = []

        db = MagicMock()
        db_cm = MagicMock()
        db_cm.__enter__ = MagicMock(return_value=db)
        db_cm.__exit__ = MagicMock(return_value=False)
        db.scalars.return_value.first.return_value = step

        approve = _unwrap(operations.approve_media_buy)
        with (
            app.test_request_context(
                "/tenant/t1/media-buy/mb_hold/approve",
                method="POST",
                data={"action": "approve"},
            ),
            patch("src.core.database.database_session.get_db_session", return_value=db_cm),
            patch("src.admin.blueprints.operations.MediaBuyRepository") as mock_mb_repo_cls,
            patch(
                "src.services.media_buy_creative_readiness.evaluate_creative_finalize_readiness_for_session",
                return_value=hold,
            ) as mock_eval,
            patch(
                "src.core.tools.media_buy_create.execute_approved_media_buy",
            ) as mock_execute,
            patch("src.admin.services.media_buy_creative_readiness.flash") as mock_flash,
            patch("flask.redirect", return_value="redirected") as mock_redirect,
            patch("flask.url_for", return_value="/detail"),
            patch("flask.session", {"user": {"email": "op@example.com"}}),
        ):
            mock_mb_repo_cls.return_value.get_by_id.return_value = media_buy
            result = approve("t1", "mb_hold")

        assert result == "redirected"
        assert media_buy.status == "pending_creatives"
        mock_eval.assert_called_once_with(db, "t1", media_buy_id="mb_hold")
        mock_execute.assert_not_called()
        # Hold path: single commit for pending_creatives + approval metadata.
        db.commit.assert_called_once_with()
        mock_flash.assert_called_once_with(hold.hold_message, "info")
        mock_redirect.assert_called_once_with("/detail")


class TestApproveWorkflowReadyArm:
    """Ready arm: compute flight status + execute_approved_media_buy are exercised."""

    def test_approve_workflow_step_ready_executes(self):
        from src.admin.app import create_app
        from src.admin.blueprints import workflows

        app = create_app()
        media_buy = MagicMock()
        media_buy.media_buy_id = "mb_ready"
        media_buy.status = "pending_approval"

        step = MagicMock()
        mapping = MagicMock()
        mapping.object_type = "media_buy"
        mapping.object_id = "mb_ready"

        db = MagicMock()
        db_cm = MagicMock()
        db_cm.__enter__ = MagicMock(return_value=db)
        db_cm.__exit__ = MagicMock(return_value=False)

        ready = CreativeFinalizeReadiness(
            ready=True,
            unapproved_creative_ids=[],
            hold_reason=None,
            hold_message=None,
        )

        approve = _unwrap(workflows.approve_workflow_step)
        with (
            app.test_request_context(
                "/tenant/t1/workflows/wf1/steps/s1/approve",
                method="POST",
            ),
            patch("src.admin.blueprints.workflows.get_db_session", return_value=db_cm),
            patch("src.admin.blueprints.workflows.WorkflowRepository") as mock_wf_repo_cls,
            patch("src.admin.blueprints.workflows.MediaBuyRepository") as mock_mb_repo_cls,
            patch(
                "src.services.media_buy_creative_readiness.evaluate_creative_finalize_readiness_for_session",
                return_value=ready,
            ) as mock_eval,
            patch(
                "src.services.media_buy_creative_readiness.compute_media_buy_status_from_flight_dates",
                return_value="scheduled",
            ) as mock_compute,
            patch(
                "src.core.tools.media_buy_create.execute_approved_media_buy",
                return_value=(True, None),
            ) as mock_execute,
            patch("src.admin.blueprints.workflows.flash") as mock_flash,
            patch("src.admin.blueprints.workflows.session", {"user": {"email": "op@example.com"}}),
        ):
            wf_repo = mock_wf_repo_cls.return_value
            wf_repo.update_status.return_value = step
            wf_repo.get_mappings_for_step.return_value = [mapping]
            mock_mb_repo_cls.return_value.get_by_id.return_value = media_buy

            response, status = approve("t1", "wf1", "s1")

        assert status == 200
        assert response.get_json()["success"] is True
        mock_eval.assert_called_once_with(db, "t1", media_buy_id="mb_ready")
        mock_execute.assert_called_once_with("mb_ready", "t1")
        mock_compute.assert_called_once_with(media_buy)
        assert media_buy.status == "scheduled"
        mock_flash.assert_called_once_with("Workflow step approved and media buy created successfully", "success")

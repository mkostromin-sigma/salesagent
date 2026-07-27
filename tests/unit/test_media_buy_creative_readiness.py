"""Unit tests for shared creative finalize-readiness predicate (#1696)."""

from unittest.mock import MagicMock, patch

import pytest

from src.admin.services.media_buy_creative_readiness import (
    CreativeFinalizeReadiness,
    compute_media_buy_status_from_flight_dates,
    evaluate_creative_finalize_readiness,
    should_hold_media_buy_for_creatives,
)
from src.core.schemas.creative import FINALIZE_READY_CREATIVE_STATUSES, CreativeStatusEnum


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
        assert result.assignment_count == 0
        assert result.unapproved_creative_ids == []
        assert result.hold_reason == "no_assignments"
        assert result.hold_message is not None
        assert "assigned" in result.hold_message
        creatives_repo.get_by_ids.assert_not_called()
        assert should_hold_media_buy_for_creatives(assignments_repo, creatives_repo, media_buy_id="mb_1") is True

    def test_all_approved_ready(self):
        assignments_repo, creatives_repo = _repos(
            [_assignment("c1"), _assignment("c2")],
            [[_creative("c1", "approved"), _creative("c2", "approved")]],
        )
        result = evaluate_creative_finalize_readiness(assignments_repo, creatives_repo, media_buy_id="mb_1")
        assert result.ready is True
        assert result.assignment_count == 2
        assert result.unapproved_creative_ids == []
        assert result.hold_reason is None
        assert result.hold_message is None
        creatives_repo.get_by_ids.assert_called_once_with(["c1", "c2"], "p1")
        assert should_hold_media_buy_for_creatives(assignments_repo, creatives_repo, media_buy_id="mb_1") is False

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
        assert result.assignment_count == 2
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


class TestComputeMediaBuyStatusFromFlightDates:
    def test_completed_when_past_end(self):
        from datetime import UTC, datetime, timedelta

        mb = MagicMock()
        mb.start_time = datetime.now(UTC) - timedelta(days=10)
        mb.end_time = datetime.now(UTC) - timedelta(days=1)
        mb.start_date = None
        mb.end_date = None
        assert compute_media_buy_status_from_flight_dates(mb) == "completed"

    def test_scheduled_when_before_start(self):
        from datetime import UTC, datetime, timedelta

        mb = MagicMock()
        mb.start_time = datetime.now(UTC) + timedelta(days=2)
        mb.end_time = datetime.now(UTC) + timedelta(days=10)
        mb.start_date = None
        mb.end_date = None
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
            assignment_count=0,
            unapproved_creative_ids=[],
            hold_reason="no_assignments",
            hold_message="Media buy approved! Waiting for creatives to be assigned and approved before creating in GAM.",
        ),
        CreativeFinalizeReadiness(
            ready=False,
            assignment_count=1,
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
        media_buy.status = "pending_approval"

        step = MagicMock()
        mapping = MagicMock()
        mapping.object_type = "media_buy"
        mapping.object_id = "mb_hold"

        db = MagicMock()
        db_cm = MagicMock()
        db_cm.__enter__ = MagicMock(return_value=db)
        db_cm.__exit__ = MagicMock(return_value=False)

        mock_assignments = MagicMock()
        mock_creatives = MagicMock()

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
                "src.admin.blueprints.workflows.CreativeAssignmentRepository",
                return_value=mock_assignments,
            ),
            patch(
                "src.admin.blueprints.workflows.CreativeRepository",
                return_value=mock_creatives,
            ),
            patch(
                "src.admin.services.media_buy_creative_readiness.evaluate_creative_finalize_readiness",
                return_value=hold,
            ) as mock_eval,
            patch(
                "src.core.tools.media_buy_create.execute_approved_media_buy",
            ) as mock_execute,
            patch("src.admin.blueprints.workflows.flash"),
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
        mock_eval.assert_called_once_with(mock_assignments, mock_creatives, media_buy_id="mb_hold")
        mock_execute.assert_not_called()
        assert db.commit.call_count >= 1

    def test_approve_media_buy_holds_without_execute(self, hold):
        from src.admin.app import create_app
        from src.admin.blueprints import operations

        app = create_app()
        media_buy = MagicMock()
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

        mock_assignments = MagicMock()
        mock_creatives = MagicMock()

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
                "src.admin.blueprints.operations.CreativeAssignmentRepository",
                return_value=mock_assignments,
            ),
            patch(
                "src.admin.blueprints.operations.CreativeRepository",
                return_value=mock_creatives,
            ),
            patch(
                "src.admin.services.media_buy_creative_readiness.evaluate_creative_finalize_readiness",
                return_value=hold,
            ) as mock_eval,
            patch(
                "src.core.tools.media_buy_create.execute_approved_media_buy",
            ) as mock_execute,
            patch("flask.flash") as mock_flash,
            patch("flask.redirect", return_value="redirected") as mock_redirect,
            patch("flask.url_for", return_value="/detail"),
            patch("flask.session", {"user": {"email": "op@example.com"}}),
        ):
            mock_mb_repo_cls.return_value.get_by_id.return_value = media_buy
            result = approve("t1", "mb_hold")

        assert result == "redirected"
        assert media_buy.status == "pending_creatives"
        mock_eval.assert_called_once_with(mock_assignments, mock_creatives, media_buy_id="mb_hold")
        mock_execute.assert_not_called()
        assert db.commit.call_count >= 1
        mock_flash.assert_called_once_with(hold.hold_message, "info")
        mock_redirect.assert_called_once_with("/detail")

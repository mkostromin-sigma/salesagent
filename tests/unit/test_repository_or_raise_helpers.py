"""Repository ``*_or_raise`` helpers: real fetch-and-raise semantics.

These exercise the actual helper logic (the plain getter + the typed not-found
raise) against a mocked SQLAlchemy session — no DB required. They back the
tool-level tests, which mock the helpers, with a test of the real behavior:
that the helper returns the entity when present and raises the correct typed
``AdCPNotFoundError`` subclass (with the id in the message) when absent.
"""

from unittest.mock import MagicMock

import pytest

from src.core.database.repositories.media_buy import MediaBuyRepository
from src.core.database.repositories.workflow import WorkflowRepository
from src.core.exceptions import (
    AdCPMediaBuyNotFoundError,
    AdCPPackageNotFoundError,
    AdCPTaskNotFoundError,
)


def _repo_with_first(repo_cls, first_value):
    """Build a repository whose ``session.scalars(...).first()`` returns ``first_value``."""
    session = MagicMock()
    session.scalars.return_value.first.return_value = first_value
    return repo_cls(session, "tenant-1")


def _compiled_last_select(session: MagicMock) -> str:
    """Compile the most recent ``session.scalars(...)`` SELECT with literal binds."""
    return str(session.scalars.call_args[0][0].compile(compile_kwargs={"literal_binds": True}))


def _expected_scoped_clause(step_id: str, tenant_id: str, principal_id: str) -> str:
    """Expected FROM..WHERE tail for a step_id/tenant/principal-scoped SELECT.

    Includes the JOIN (not just the WHERE tail): ``select(...).join(DBContext).where(*c)``
    and ``select(...).where(*c)`` (a cartesian product that re-leaks across tenants)
    produce byte-identical WHERE tails, so a WHERE-only slice cannot see a dropped
    ``.join(DBContext)``.
    """
    return (
        "workflow_steps JOIN contexts ON contexts.context_id = workflow_steps.context_id \n"
        f"WHERE workflow_steps.step_id = '{step_id}' AND contexts.tenant_id = '{tenant_id}' "
        f"AND contexts.principal_id = '{principal_id}'"
    )


class TestMediaBuyOrRaise:
    def test_get_by_id_or_raise_returns_when_present(self):
        media_buy = MagicMock()
        repo = _repo_with_first(MediaBuyRepository, media_buy)
        assert repo.get_by_id_or_raise("mb-1") is media_buy

    def test_get_by_id_or_raise_raises_when_absent(self):
        repo = _repo_with_first(MediaBuyRepository, None)
        with pytest.raises(AdCPMediaBuyNotFoundError) as exc:
            repo.get_by_id_or_raise("mb-missing")
        assert exc.value.error_code == "MEDIA_BUY_NOT_FOUND"
        assert "mb-missing" in str(exc.value)

    def test_get_by_id_or_raise_echoes_context_into_envelope(self):
        """context= is carried onto the raised error AND echoed into the wire envelope.

        Not just accepted: a regression that takes ``context=`` and drops it would
        still satisfy a signature-only test. Assert the value lands on the exception
        and survives into the two-layer envelope (assert_envelope_shape does not
        check context, so we assert envelope["context"] directly).
        """
        from src.core.exceptions import build_two_layer_error_envelope

        repo = _repo_with_first(MediaBuyRepository, None)
        ctx = {"context_id": "ctx-9"}
        with pytest.raises(AdCPMediaBuyNotFoundError) as exc:
            repo.get_by_id_or_raise("mb-missing", context=ctx)

        assert exc.value.context == ctx
        assert build_two_layer_error_envelope(exc.value)["context"] == ctx

    def test_get_package_or_raise_returns_when_present(self):
        package = MagicMock()
        repo = _repo_with_first(MediaBuyRepository, package)
        assert repo.get_package_or_raise("mb-1", "pkg-1") is package

    def test_get_package_or_raise_raises_when_absent(self):
        repo = _repo_with_first(MediaBuyRepository, None)
        with pytest.raises(AdCPPackageNotFoundError) as exc:
            repo.get_package_or_raise("mb-1", "pkg-missing")
        assert exc.value.error_code == "PACKAGE_NOT_FOUND"
        assert "pkg-missing" in str(exc.value)


class TestWorkflowOrRaise:
    def test_get_by_step_id_or_raise_returns_when_present(self):
        step = MagicMock()
        repo = _repo_with_first(WorkflowRepository, step)
        assert repo.get_by_step_id_or_raise("step-1", principal_id="principal-a") is step

    def test_get_by_step_id_or_raise_raises_when_absent(self):
        repo = _repo_with_first(WorkflowRepository, None)
        with pytest.raises(AdCPTaskNotFoundError) as exc:
            repo.get_by_step_id_or_raise("step-missing", principal_id="principal-a")
        assert exc.value.error_code == "TASK_NOT_FOUND"
        assert str(exc.value) == "Reference not found"

    def test_get_by_step_id_or_raise_forwards_principal_id(self):
        """Buyer or_raise always forwards principal_id into get_by_step_id.

        Sibling ownership (row exists, wrong principal) is graded by SQL compile
        + integration; this unit only locks the forwarding contract so a
        regression that drops principal_id= cannot stay green here.
        """
        session = MagicMock()
        session.scalars.return_value.first.return_value = None
        repo = WorkflowRepository(session, "tenant-1")
        with pytest.raises(AdCPTaskNotFoundError):
            repo.get_by_step_id_or_raise("step-1", principal_id="sibling-b")
        compiled = _compiled_last_select(session)
        assert compiled.split("FROM", 1)[1].strip() == _expected_scoped_clause("step-1", "tenant-1", "sibling-b")

    def test_get_by_step_id_or_raise_rejects_falsy_principal_id(self):
        """Explicit None/empty must not silently tenant-scope via get_by_step_id."""
        repo = _repo_with_first(WorkflowRepository, MagicMock())
        with pytest.raises(ValueError, match="principal_id is required"):
            repo.get_by_step_id_or_raise("step-1", principal_id=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="principal_id is required"):
            repo.get_by_step_id_or_raise("step-1", principal_id="")
        repo._session.scalars.assert_not_called()

    def test_get_by_step_id_filters_principal_in_sql(self):
        """Principal filter is applied in the WHERE clause (not post-fetch)."""
        session = MagicMock()
        session.scalars.return_value.first.return_value = None
        repo = WorkflowRepository(session, "tenant-1")
        repo.get_by_step_id("step-1", principal_id="principal-a")
        compiled = _compiled_last_select(session)
        assert compiled.split("FROM", 1)[1].strip() == _expected_scoped_clause("step-1", "tenant-1", "principal-a")

    def test_update_status_sibling_principal_returns_none(self):
        """Write-side ownership: sibling principal_id must not update the row.

        Grades the SQL seam (same scoped WHERE as read), not a mocked kwarg
        forward. Reverting update_status to tenant-only get_by_step_id must fail.
        """
        session = MagicMock()
        session.scalars.return_value.first.return_value = None
        repo = WorkflowRepository(session, "tenant-1")
        assert repo.update_status("step-1", status="completed", principal_id="sibling-b") is None
        compiled = _compiled_last_select(session)
        assert compiled.split("FROM", 1)[1].strip() == _expected_scoped_clause("step-1", "tenant-1", "sibling-b")

    def test_update_status_owner_principal_updates(self):
        step = MagicMock()
        session = MagicMock()
        session.scalars.return_value.first.return_value = step
        repo = WorkflowRepository(session, "tenant-1")
        assert repo.update_status("step-1", status="completed", principal_id="owner-a") is step
        assert step.status == "completed"
        session.flush.assert_called_once()
        compiled = _compiled_last_select(session)
        assert compiled.split("FROM", 1)[1].strip() == _expected_scoped_clause("step-1", "tenant-1", "owner-a")

    def test_get_by_step_id_or_raise_default_message_from_spec_supplement(self):
        """Argument-less raise must still emit the REFERENCE_NOT_FOUND uniform message."""
        from src.core.exceptions import _SPEC_SUPPLEMENT_CODES

        repo = _repo_with_first(WorkflowRepository, None)
        with pytest.raises(AdCPTaskNotFoundError) as exc:
            repo.get_by_step_id_or_raise("step-missing", principal_id="principal-a")
        assert str(exc.value) == _SPEC_SUPPLEMENT_CODES["REFERENCE_NOT_FOUND"]["message"]

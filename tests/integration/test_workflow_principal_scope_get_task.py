"""Integration: durable get_by_step_id_or_raise / get_task / complete_task are principal-scoped.

AdCP 3.1.1 L1 Agent and Account Isolation — bind on create, verify on access;
same REFERENCE_NOT_FOUND on the wire for sibling principal as for unknown id
(pinned enums/error-code.json @ 3.1.1: typed task_id that does not exist
or is not accessible). UC-027 BDD grades the wire path; this module grades
the in-process repository + tool contract.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session as SASession

from src.core.database.database_session import get_engine
from src.core.database.repositories.workflow import WorkflowRepository
from src.core.exceptions import AdCPTaskNotFoundError
from src.core.resolved_identity import ResolvedIdentity
from src.core.tools.task_management import complete_task, get_task
from tests.factories import PrincipalFactory, TenantFactory
from tests.utils.database_helpers import _bind_factories_to_session
from tests.utils.workflow_task_seed import create_principal_owned_workflow_step

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _identity(tenant_id: str, principal_id: str) -> ResolvedIdentity:
    return PrincipalFactory.make_identity(
        principal_id=principal_id,
        tenant_id=tenant_id,
        protocol="mcp",
    )


def _assert_same_not_found(
    sibling_exc: pytest.ExceptionInfo[AdCPTaskNotFoundError],
    missing_exc: pytest.ExceptionInfo[AdCPTaskNotFoundError],
) -> None:
    """Sibling-principal denial must be wire-indistinguishable from unknown id.

    Asserts the buyer-facing wire code (REFERENCE_NOT_FOUND), not the internal
    TASK_NOT_FOUND taxonomy, plus the generic (non resource-qualified) message.
    """
    assert sibling_exc.value.wire_error_code == missing_exc.value.wire_error_code == "REFERENCE_NOT_FOUND"
    assert sibling_exc.value.error_code == missing_exc.value.error_code == "TASK_NOT_FOUND"
    assert str(sibling_exc.value) == str(missing_exc.value) == "Reference not found"


@pytest.fixture
def principal_scoped_step(integration_db):
    """Two principals in one tenant; durable workflow steps under principal A.

    The sibling also owns a durable step (``sibling_step_id``) — without it,
    the sibling has zero ``contexts`` rows in this tenant, so a regression
    that drops ``.join(DBContext)`` (cartesian product on ``WorkflowStep`` x
    ``Context``) would match zero rows either way and the sibling-denial
    tests below would pass for the wrong reason. Seeding a sibling-owned row
    means a mis-paired cartesian match can actually leak the owner's step to
    the sibling, so the tests genuinely exercise the leak path.
    """
    engine = get_engine()
    with SASession(bind=engine) as session, _bind_factories_to_session(session):
        tenant = TenantFactory(tenant_id="pscope_tenant_get_task")
        owner = PrincipalFactory(
            tenant=tenant,
            principal_id="pscope_owner",
            platform_mappings={"mock": {"id": "pscope_owner_adv"}},
        )
        sibling = PrincipalFactory(
            tenant=tenant,
            principal_id="pscope_sibling",
            platform_mappings={"mock": {"id": "pscope_sibling_adv"}},
        )
        step = create_principal_owned_workflow_step(
            tenant_id=tenant.tenant_id,
            principal_id=owner.principal_id,
            status="completed",
        )
        pending = create_principal_owned_workflow_step(
            tenant_id=tenant.tenant_id,
            principal_id=owner.principal_id,
            status="requires_approval",
        )
        sibling_step = create_principal_owned_workflow_step(
            tenant_id=tenant.tenant_id,
            principal_id=sibling.principal_id,
            status="completed",
        )
        yield {
            "tenant_id": tenant.tenant_id,
            "owner_principal_id": owner.principal_id,
            "sibling_principal_id": sibling.principal_id,
            "step_id": step.step_id,
            "pending_step_id": pending.step_id,
            "sibling_step_id": sibling_step.step_id,
            "session": session,
        }


def test_owner_can_fetch_step(principal_scoped_step):
    data = principal_scoped_step
    repo = WorkflowRepository(data["session"], data["tenant_id"])
    step = repo.get_by_step_id_or_raise(
        data["step_id"],
        principal_id=data["owner_principal_id"],
    )
    assert step.step_id == data["step_id"]


def test_sibling_principal_same_error_as_unknown(principal_scoped_step):
    data = principal_scoped_step
    repo = WorkflowRepository(data["session"], data["tenant_id"])
    with pytest.raises(AdCPTaskNotFoundError) as sibling_exc:
        repo.get_by_step_id_or_raise(
            data["step_id"],
            principal_id=data["sibling_principal_id"],
        )
    with pytest.raises(AdCPTaskNotFoundError) as missing_exc:
        repo.get_by_step_id_or_raise(
            "step_does_not_exist",
            principal_id=data["owner_principal_id"],
        )
    _assert_same_not_found(sibling_exc, missing_exc)


def test_tenant_only_lookup_still_works_without_principal(principal_scoped_step):
    """Admin/service path: get_by_step_id without principal_id remains tenant-scoped."""
    data = principal_scoped_step
    repo = WorkflowRepository(data["session"], data["tenant_id"])
    step = repo.get_by_step_id(data["step_id"])
    assert step is not None
    assert step.step_id == data["step_id"]


@pytest.mark.asyncio
async def test_get_task_owner_ok_sibling_same_as_unknown(principal_scoped_step):
    data = principal_scoped_step
    owner = _identity(data["tenant_id"], data["owner_principal_id"])
    sibling = _identity(data["tenant_id"], data["sibling_principal_id"])

    detail = await get_task(task_id=data["step_id"], identity=owner)
    assert detail["task_id"] == data["step_id"]

    with pytest.raises(AdCPTaskNotFoundError) as sibling_exc:
        await get_task(task_id=data["step_id"], identity=sibling)
    with pytest.raises(AdCPTaskNotFoundError) as missing_exc:
        await get_task(task_id="step_does_not_exist", identity=owner)
    _assert_same_not_found(sibling_exc, missing_exc)


@pytest.mark.asyncio
async def test_complete_task_owner_ok_sibling_same_as_unknown(principal_scoped_step):
    data = principal_scoped_step
    owner = _identity(data["tenant_id"], data["owner_principal_id"])
    sibling = _identity(data["tenant_id"], data["sibling_principal_id"])

    with pytest.raises(AdCPTaskNotFoundError) as sibling_exc:
        await complete_task(task_id=data["pending_step_id"], identity=sibling)
    with pytest.raises(AdCPTaskNotFoundError) as missing_exc:
        await complete_task(task_id="step_does_not_exist", identity=owner)
    _assert_same_not_found(sibling_exc, missing_exc)

    result = await complete_task(task_id=data["pending_step_id"], status="completed", identity=owner)
    assert result["task_id"] == data["pending_step_id"]
    assert result["status"] == "completed"


def test_update_status_write_predicate_denies_sibling(principal_scoped_step):
    """Repository update_status with sibling principal_id returns None (KM Aug-05).

    Grades the write-side SQL ownership seam directly — not a mock kwarg proxy.
    """
    data = principal_scoped_step
    engine = get_engine()
    with SASession(bind=engine) as session:
        repo = WorkflowRepository(session, data["tenant_id"])
        denied = repo.update_status(
            data["pending_step_id"],
            status="completed",
            principal_id=data["sibling_principal_id"],
        )
        assert denied is None
        updated = repo.update_status(
            data["pending_step_id"],
            status="completed",
            principal_id=data["owner_principal_id"],
        )
        assert updated is not None
        assert updated.status == "completed"
        session.commit()


def test_get_task_a2a_and_mcp_success_path(integration_db):
    """Success-path oracle: A2A + MCP return a parseable get_task payload (#1812).

    Sync test: TaskEnv.call_a2a / call_mcp drive ``asyncio.run`` internally —
    must not nest under ``@pytest.mark.asyncio``.
    """
    from tests.harness.task_management import GetTaskWireResponse, TaskEnv

    with TaskEnv(tenant_id="pscope_wire_ok", principal_id="wire_owner") as env:
        tenant = TenantFactory(tenant_id="pscope_wire_ok")
        PrincipalFactory(
            tenant=tenant,
            principal_id="wire_owner",
            platform_mappings={"mock": {"id": "wire_owner_adv"}},
        )
        env._commit_factory_data()
        step_id = env.seed_owner_task(principal_id="wire_owner", status="completed")
        a2a = env.call_a2a(tool="get_task", task_id=step_id)
        assert isinstance(a2a, GetTaskWireResponse)
        assert a2a.task_id == step_id
        mcp = env.call_mcp(tool="get_task", task_id=step_id)
        assert isinstance(mcp, GetTaskWireResponse)
        assert mcp.task_id == step_id

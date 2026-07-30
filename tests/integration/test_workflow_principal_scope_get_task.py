"""Integration: durable get_by_step_id_or_raise is principal-scoped (#1741).

AdCP 3.1.1 L1 Agent and Account Isolation — bind on create, verify on access;
same TASK_NOT_FOUND for sibling principal as for unknown id (ungraded for
sibling-principal; UC-027 grades cross-tenant only).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session as SASession

from src.core.context_manager import ContextManager
from src.core.database.database_session import get_engine
from src.core.database.repositories.workflow import WorkflowRepository
from src.core.exceptions import AdCPTaskNotFoundError
from tests.factories import ALL_FACTORIES, PrincipalFactory, TenantFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.fixture
def principal_scoped_step(integration_db):
    """Two principals in one tenant; one durable workflow step under principal A."""
    engine = get_engine()
    session = SASession(bind=engine)
    for factory in ALL_FACTORIES:
        factory._meta.sqlalchemy_session = session

    try:
        tenant = TenantFactory(tenant_id="pscope_tenant_1741")
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
        cm = ContextManager()
        context = cm.create_context(
            tenant_id=tenant.tenant_id,
            principal_id=owner.principal_id,
        )
        step = cm.create_workflow_step(
            context_id=context.context_id,
            step_type="tool_call",
            owner="principal",
            status="completed",
            tool_name="create_media_buy",
            request_data={"budget": 1000},
        )
        yield {
            "tenant_id": tenant.tenant_id,
            "owner_principal_id": owner.principal_id,
            "sibling_principal_id": sibling.principal_id,
            "step_id": step.step_id,
            "session": session,
        }
    finally:
        for factory in ALL_FACTORIES:
            factory._meta.sqlalchemy_session = None
        session.close()


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
    assert sibling_exc.value.error_code == missing_exc.value.error_code == "TASK_NOT_FOUND"
    assert str(sibling_exc.value) == f"Task {data['step_id']} not found"
    assert str(missing_exc.value) == "Task step_does_not_exist not found"


def test_tenant_only_lookup_still_works_without_principal(principal_scoped_step):
    """Admin/service path: get_by_step_id without principal_id remains tenant-scoped."""
    data = principal_scoped_step
    repo = WorkflowRepository(data["session"], data["tenant_id"])
    step = repo.get_by_step_id(data["step_id"])
    assert step is not None
    assert step.step_id == data["step_id"]

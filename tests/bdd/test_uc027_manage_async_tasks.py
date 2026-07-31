"""BDD scenarios for UC-027: manage async tasks (get_task / complete_task).

Binds BR-UC-027 (whole feature; unwired scenarios xfail at the harness fixture)
and the locally-added sibling-principal isolation feature (#1812 review).

Wired today: ``@sibling-principal`` scenarios that grade same-tenant
sibling-principal denial as wire ``REFERENCE_NOT_FOUND`` indistinguishable from
an unknown ``task_id`` — asserted via ``TransportResult.assert_wire_error`` on
A2A + MCP (no REST route for these tools; see ``_NO_REST_UC_TAG_PREFIXES``).

Spec grounding (AdCP 3.1.1 enums/error-code.json ``REFERENCE_NOT_FOUND``):
typed ``task_id`` that does not exist or is not accessible MUST emit the same
wire code; uniform-response MUST for "exists but caller lacks access".
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.steps.generic._auth import authenticate_env_as
from tests.bdd.steps.generic._dispatch import dispatch_request
from tests.factories import PrincipalFactory, TenantFactory

# Whole-feature binding is the repo convention the CI shard-splitter requires.
scenarios("features/BR-UC-027-manage-async-tasks.feature")
scenarios("features/local-uc027-sibling-principal-isolation.feature")

_OWNER = "owner_principal"
_SIBLING = "sibling_principal"
_UNKNOWN_TASK_ID = "step_does_not_exist"


def _seed_owner_task(ctx: dict, *, status: str) -> str:
    """Create a durable workflow step owned by the owner principal; return step_id."""
    from src.core.context_manager import ContextManager

    env = ctx["env"]
    env._commit_factory_data()
    cm = ContextManager()
    context = cm.create_context(
        tenant_id=ctx["tenant"].tenant_id,
        principal_id=ctx["owner_principal_id"],
    )
    step = cm.create_workflow_step(
        context_id=context.context_id,
        step_type="approval" if status == "requires_approval" else "tool_call",
        owner="principal",
        status=status,
        tool_name="create_media_buy",
        request_data={"budget": 1000},
    )
    ctx["owner_task_id"] = step.step_id
    return step.step_id


@given("an owner principal and a sibling principal in the same tenant")
def given_owner_and_sibling_principals(ctx: dict) -> None:
    """Two principals in one fresh tenant (sibling-principal isolation precondition)."""
    env = ctx["env"]
    tenant_id = f"uc027_iso_{uuid4().hex[:8]}"
    tenant = TenantFactory(tenant_id=tenant_id)
    env.switch_tenant(tenant_id)
    PrincipalFactory(tenant=tenant, principal_id=_OWNER)
    PrincipalFactory(tenant=tenant, principal_id=_SIBLING)
    env._commit_factory_data()
    ctx["tenant"] = tenant
    ctx["owner_principal_id"] = _OWNER
    ctx["sibling_principal_id"] = _SIBLING


@given(parsers.parse('the owner has a durable workflow task "{label}"'))
def given_owner_durable_task(ctx: dict, label: str) -> None:
    """Seed a completed durable task under the owner (label is Gherkin-only)."""
    ctx["task_label"] = label
    _seed_owner_task(ctx, status="completed")


@given(parsers.parse('the owner has a durable pending workflow task "{label}"'))
def given_owner_durable_pending_task(ctx: dict, label: str) -> None:
    """Seed a pending (completable) durable task under the owner."""
    ctx["task_label"] = label
    _seed_owner_task(ctx, status="requires_approval")


@when("the sibling principal invokes get_task for the owner's task")
def when_sibling_invokes_get_task(ctx: dict) -> None:
    """Authenticate as sibling and dispatch get_task for the owner's task_id."""
    authenticate_env_as(ctx, ctx["sibling_principal_id"])
    ctx["task_tool"] = "get_task"
    dispatch_request(ctx, tool="get_task", task_id=ctx["owner_task_id"])


@when("the sibling principal invokes complete_task for the owner's task")
def when_sibling_invokes_complete_task(ctx: dict) -> None:
    """Authenticate as sibling and dispatch complete_task for the owner's task_id."""
    authenticate_env_as(ctx, ctx["sibling_principal_id"])
    ctx["task_tool"] = "complete_task"
    dispatch_request(
        ctx,
        tool="complete_task",
        task_id=ctx["owner_task_id"],
        status="completed",
    )


@then("the wire error is REFERENCE_NOT_FOUND matching an unknown task_id")
def then_wire_error_matches_unknown_task(ctx: dict) -> None:
    """Sibling denial must be wire-indistinguishable from unknown task_id.

    Grades buyer-facing ``REFERENCE_NOT_FOUND`` via ``assert_wire_error`` on the
    sibling result and on a fresh unknown-id call as the owner (same transport).
    """
    sibling_result = ctx["result"]
    assert sibling_result is not None, "Expected sibling dispatch TransportResult on ctx['result']"
    sibling_result.assert_wire_error("REFERENCE_NOT_FOUND")

    tool = ctx["task_tool"]
    authenticate_env_as(ctx, ctx["owner_principal_id"])
    kwargs: dict[str, Any] = {"tool": tool, "task_id": _UNKNOWN_TASK_ID}
    if tool == "complete_task":
        kwargs["status"] = "completed"
    dispatch_request(ctx, **kwargs)
    unknown_result = ctx["result"]
    assert unknown_result is not None, "Expected unknown-id dispatch TransportResult"
    unknown_result.assert_wire_error("REFERENCE_NOT_FOUND")

    sib_code = (sibling_result.wire_error_envelope or {}).get("adcp_error", {}).get("code")
    unk_code = (unknown_result.wire_error_envelope or {}).get("adcp_error", {}).get("code")
    assert sib_code == unk_code == "REFERENCE_NOT_FOUND"

"""UC-027 manage async tasks — sibling-principal isolation steps (local feature).

Moved out of ``tests/bdd/test_uc027_manage_async_tasks.py`` so the sixteen
BDD step-scanning guards see these definitions (#1812 ChrisHuie review).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pytest_bdd import given, parsers, then, when

from tests.bdd.steps._outcome_helpers import wire_field
from tests.bdd.steps.generic._auth import authenticate_env_as
from tests.bdd.steps.generic._dispatch import dispatch_request
from tests.factories import PrincipalFactory, TenantFactory

_OWNER = "owner_principal"
_SIBLING = "sibling_principal"
_UNKNOWN_TASK_ID = "step_does_not_exist"


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
    env = ctx["env"]
    ctx["owner_task_id"] = env.seed_owner_task(principal_id=ctx["owner_principal_id"], status="completed")


@given(parsers.parse('the owner has a durable pending workflow task "{label}"'))
def given_owner_durable_pending_task(ctx: dict, label: str) -> None:
    """Seed a pending (completable) durable task under the owner."""
    ctx["task_label"] = label
    env = ctx["env"]
    ctx["owner_task_id"] = env.seed_owner_task(
        principal_id=ctx["owner_principal_id"],
        status="requires_approval",
    )


@when("the owner principal invokes get_task for their task")
def when_owner_invokes_get_task(ctx: dict) -> None:
    """Owner success control — proves the seed is reachable before sibling denial."""
    authenticate_env_as(ctx, ctx["owner_principal_id"])
    ctx["task_tool"] = "get_task"
    dispatch_request(ctx, tool="get_task", task_id=ctx["owner_task_id"])


@when("the owner principal invokes complete_task for their pending task")
def when_owner_invokes_complete_task(ctx: dict) -> None:
    """Owner success control — runs the real ``complete_task`` success leg on the wire.

    Completing the task here does not break the later sibling-denial check:
    ownership is filtered on ``contexts.principal_id`` in the SQL WHERE
    clause, so a sibling's ``complete_task`` attempt on this same task_id
    still returns REFERENCE_NOT_FOUND (row invisible to them) regardless of
    the task's resulting status — the row is never visible for them to hit
    a "already completed" conflict instead.
    """
    authenticate_env_as(ctx, ctx["owner_principal_id"])
    ctx["task_tool"] = "complete_task"
    dispatch_request(ctx, tool="complete_task", task_id=ctx["owner_task_id"], status="completed")


@then("the wire returns the owner's task_id")
def then_wire_returns_owner_task_id(ctx: dict) -> None:
    """Owner success leg — ``wire_field`` grades the real success-path wire."""
    assert wire_field(ctx, "task_id") == ctx["owner_task_id"]


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


@when("an unknown task_id is requested as the owner for the same tool")
def when_unknown_task_id_as_owner(ctx: dict) -> None:
    """Unknown-id control dispatch (same tool + transport as the sibling denial)."""
    tool = ctx["task_tool"]
    authenticate_env_as(ctx, ctx["owner_principal_id"])
    kwargs: dict[str, Any] = {"tool": tool, "task_id": _UNKNOWN_TASK_ID}
    if tool == "complete_task":
        kwargs["status"] = "completed"
    # Preserve sibling result before overwrite for the Then comparison.
    ctx["sibling_result"] = ctx["result"]
    dispatch_request(ctx, **kwargs)
    ctx["unknown_result"] = ctx["result"]


@then("the wire error is REFERENCE_NOT_FOUND matching an unknown task_id")
def then_wire_error_matches_unknown_task(ctx: dict) -> None:
    """Sibling denial must be wire-indistinguishable from unknown task_id.

    Grades buyer-facing ``REFERENCE_NOT_FOUND`` via ``assert_wire_error`` on both
    results (same transport), *and* asserts the full two-layer envelopes are
    byte-equal — a single-query ownership oracle that emits a distinguishing
    ``suggestion``/``field``/``message`` on denial would still pass the two
    independent ``assert_wire_error`` calls, so equality is what closes every
    channel (code, message, field, details, suggestion) in one line.
    """
    assert "sibling_result" in ctx, "Expected sibling dispatch to have set sibling_result"
    sibling_result = ctx["sibling_result"]
    unknown_result = ctx.get("unknown_result")
    sibling_result.assert_wire_error("REFERENCE_NOT_FOUND")
    assert unknown_result is not None, "Expected unknown-id dispatch TransportResult"
    unknown_result.assert_wire_error("REFERENCE_NOT_FOUND")
    assert sibling_result.wire_error_envelope == unknown_result.wire_error_envelope

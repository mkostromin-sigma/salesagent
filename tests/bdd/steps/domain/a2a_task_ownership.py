"""BDD steps for the A2A in-memory task ownership gate (local feature, #1780).

Grades the protocol surface of ``tasks/get`` / ``tasks/cancel``, not an AdCP
skill: a denial has no artifact and no two-layer envelope. The oracle is
``assert_wire_task_not_found`` on the **live** v0.3 JSON-RPC body captured
through ``create_jsonrpc_routes(..., enable_v0_3_compat=True)``. Live adapter
wire serialization is also proven in ``tests/unit/test_a2a_task_identity_wire.py``
(#1720). Never use ``assert_envelope_shape`` / ``wire_error_envelope`` here.

Owner, same-tenant sibling and other-tenant caller are seeded by
``A2ATaskOwnershipEnv`` with real access tokens, so every dispatch runs the
production auth chain before the ownership gate decides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pytest_bdd import given, parsers, then, when

from tests.a2a_helpers import (
    OWNED_TASK_FORBIDDEN_SUBSTRINGS,
    assert_no_identity_leak,
    assert_wire_task_not_found,
)

if TYPE_CHECKING:
    from a2a.types import TaskState

_OWNED_TASK_STATE_BY_NAME = {
    "WORKING": "TASK_STATE_WORKING",
    "CANCELED": "TASK_STATE_CANCELED",
}

# v0.3 compat wire uses lowercase TaskState values (``working`` / ``canceled``).
_WIRE_TASK_STATE_BY_NAME = {
    "WORKING": "working",
    "CANCELED": "canceled",
}


def _task_state(name: str) -> TaskState:
    """Resolve a Gherkin state word to the protobuf ``TaskState`` enum member."""
    from a2a.types import TaskState

    return getattr(TaskState, _OWNED_TASK_STATE_BY_NAME[name])


def _wire_task_state(name: str):
    """Resolve a Gherkin state word to the v0.3 wire ``TaskState`` enum member."""
    from a2a.compat.v0_3.types import TaskState as WireTaskState

    return WireTaskState(_WIRE_TASK_STATE_BY_NAME[name])


@given(parsers.parse('an in-memory A2A task "{task_id}" owned by the owning principal'))
def given_owned_in_memory_task(ctx: dict, task_id: str) -> None:
    """Seed a WORKING task owned by the owner principal on the shared handler."""
    env = ctx["env"]
    env.seed_a2a_task(task_id, identity=env.identity_for_role("owner"))


@given(parsers.parse('an in-memory A2A task "{task_id}" with no owner record'))
def given_orphan_in_memory_task(ctx: dict, task_id: str) -> None:
    """Seed a task in ``handler.tasks`` without recording ``_task_owners``.

    Grades the fail-closed path when a task exists but has no owner record
    (legacy tasks / future create paths that forget to record ownership).
    """
    env = ctx["env"]
    env.seed_a2a_task(task_id, identity=env.identity_for_role("owner"), record_owner=False)


@when(parsers.parse('the "{role}" calls {method} for task "{task_id}"'))
def when_role_calls_task_method(ctx: dict, role: str, method: str, task_id: str) -> None:
    """Dispatch ``tasks/get`` / ``tasks/cancel`` as *role* against the shared handler."""
    env = ctx["env"]
    env.run_a2a_task_method(method, task_id, identity=env.identity_for_role(role))


@when(parsers.parse('an unauthenticated caller calls {method} for task "{task_id}"'))
def when_unauth_calls_task_method(ctx: dict, method: str, task_id: str) -> None:
    """Dispatch with ``identity=None`` — auth failure must not collapse to not-found."""
    env = ctx["env"]
    env.run_a2a_task_method(method, task_id, identity=None)


@then(parsers.parse('the A2A task response should carry task "{task_id}" in state {state}'))
def then_task_served(ctx: dict, task_id: str, state: str) -> None:
    """Assert the caller was served the Task from the live wire result.

    Grades ``body["result"]`` (v0.3 compat serialization) — not the handler's
    in-memory store — so a success-path adapter regression reddens owner-get
    and owner-cancel. Store mutation is graded separately by the stored-task Then.
    """
    env = ctx["env"]
    assert env.last_a2a_task_error is None, (
        f"Expected task {task_id} to be served, got error: {env.last_a2a_task_error}"
    )
    task = env.last_a2a_task
    assert task is not None, f"No Task returned for {task_id}"
    assert task.id == task_id
    assert task.status.state == _wire_task_state(state)


@then(parsers.parse('the A2A task response should be a JSON-RPC task-not-found error for "{task_id}"'))
def then_task_not_found(ctx: dict, task_id: str) -> None:
    """Assert the live not-found body — the same for denial and unknown id.

    ``assert_wire_task_not_found`` pins code/message/data literally on the
    captured JSON-RPC error (independent of ``_task_not_found_message``). An
    ownership denial that leaked "you may not touch this" would fail the exact
    equality. Identity leaks are graded via ``assert_no_identity_leak`` with the
    shared ``OWNED_TASK_FORBIDDEN_SUBSTRINGS`` needle set.
    """
    env = ctx["env"]
    assert env.last_a2a_task is None, f"Denied call still returned a Task: {env.last_a2a_task}"
    error = env.last_a2a_task_error
    assert error is not None, f"Expected a task-not-found error for {task_id}, got none"
    assert_wire_task_not_found(error, task_id)
    assert_no_identity_leak(
        error.get("message", ""),
        error.get("data"),
        OWNED_TASK_FORBIDDEN_SUBSTRINGS,
    )


@then("the A2A task response should be an authentication failure, not task-not-found")
def then_auth_failure_not_task_not_found(ctx: dict) -> None:
    """Unauthenticated callers must not receive the ownership-denial not-found shape."""
    env = ctx["env"]
    assert env.last_a2a_task is None, f"Unauth call still returned a Task: {env.last_a2a_task}"
    error = env.last_a2a_task_error
    assert error is not None, "Expected an authentication failure, got none"
    assert error.get("message") == "Missing authentication token", f"expected auth-failure message, got {error!r}"
    assert "Task not found" not in str(error.get("message", ""))


@then(parsers.parse('the stored task "{task_id}" should be in state {state}'))
def then_stored_task_state(ctx: dict, task_id: str, state: str) -> None:
    """Read the handler's stored Task back — a denied cancel must not mutate it."""
    env = ctx["env"]
    assert env.a2a_task_state(task_id) == _task_state(state)

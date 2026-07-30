"""BDD steps for the A2A in-memory task ownership gate (local feature, #1780).

Grades the protocol surface of ``tasks/get`` / ``tasks/cancel``, not an AdCP
skill: a denial has no artifact and no two-layer envelope. The oracle is
``assert_wire_task_not_found`` on the **handler-exception shape mirrored** to
today's v0.3 JSON-RPC body (``CoreInternalError`` / ``-32603``) — not a live
capture through ``create_jsonrpc_routes``. Live adapter wire serialization is
proven in ``tests/unit/test_a2a_task_identity_wire.py`` (#1720). Never use
``assert_envelope_shape`` / ``wire_error_envelope`` here.

Owner, same-tenant sibling and other-tenant caller are seeded by
``A2ATaskOwnershipEnv`` with real access tokens, so every dispatch runs the
production auth chain before the ownership gate decides.
"""

from __future__ import annotations

from typing import Any

from pytest_bdd import given, parsers, then, when

from tests.a2a_helpers import assert_wire_task_not_found

_OWNED_TASK_STATE_BY_NAME = {
    "WORKING": "TASK_STATE_WORKING",
    "CANCELED": "TASK_STATE_CANCELED",
}


def _task_state(name: str) -> Any:
    """Resolve a Gherkin state word to the a2a ``TaskState`` enum member."""
    from a2a.types import TaskState

    return getattr(TaskState, _OWNED_TASK_STATE_BY_NAME[name])


@given(parsers.parse('an in-memory A2A task "{task_id}" owned by the owning principal'))
def given_owned_in_memory_task(ctx: dict, task_id: str) -> None:
    """Seed a WORKING task owned by the owner principal on the shared handler."""
    env = ctx["env"]
    env.seed_a2a_task(task_id, identity=env.identity_for_role("owner"))


@when(parsers.parse('the "{role}" calls tasks/get for task "{task_id}"'))
def when_role_calls_tasks_get(ctx: dict, role: str, task_id: str) -> None:
    """Dispatch tasks/get as *role* against the shared handler."""
    env = ctx["env"]
    env._run_a2a_task_method("tasks/get", task_id, identity=env.identity_for_role(role))


@when(parsers.parse('the "{role}" calls tasks/cancel for task "{task_id}"'))
def when_role_calls_tasks_cancel(ctx: dict, role: str, task_id: str) -> None:
    """Dispatch tasks/cancel as *role* against the shared handler."""
    env = ctx["env"]
    env._run_a2a_task_method("tasks/cancel", task_id, identity=env.identity_for_role(role))


@then(parsers.parse('the A2A task response should carry task "{task_id}" in state {state}'))
def then_task_served(ctx: dict, task_id: str, state: str) -> None:
    """Assert the caller was served the Task itself — id and state both graded."""
    env = ctx["env"]
    assert env.last_a2a_task_error is None, (
        f"Expected task {task_id} to be served, got error: {env.last_a2a_task_error}"
    )
    task = env.last_a2a_task
    assert task is not None, f"No Task returned for {task_id}"
    assert task.id == task_id
    assert task.status.state == _task_state(state)


@then(parsers.parse('the A2A task response should be a JSON-RPC task-not-found error for "{task_id}"'))
def then_task_not_found(ctx: dict, task_id: str) -> None:
    """Assert the mirrored not-found body — the same for denial and unknown id.

    ``assert_wire_task_not_found`` pins code/message/data literally on the
    handler-exception reconstruction (not a live JSON-RPC response). An
    ownership denial that leaked "you may not touch this" (or any tenant /
    principal identifier) would fail here rather than pass as "some error".
    """
    env = ctx["env"]
    assert env.last_a2a_task is None, f"Denied call still returned a Task: {env.last_a2a_task}"
    error = env.last_a2a_task_error
    assert error is not None, f"Expected a task-not-found error for {task_id}, got none"
    assert_wire_task_not_found(error, task_id)


@then(parsers.parse('the stored task "{task_id}" should be in state {state}'))
def then_stored_task_state(ctx: dict, task_id: str, state: str) -> None:
    """Read the handler's stored Task back — a denied cancel must not mutate it."""
    env = ctx["env"]
    assert env.a2a_task_state(task_id) == _task_state(state)

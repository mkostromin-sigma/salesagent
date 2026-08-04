"""In-process JSON-RPC wire grade for A2A task ownership (#1702 / #1720).

live_server xfails only POST an unknown id — they never hit the owner-compare
branch. Routes through the shared ``build_a2a_jsonrpc_client`` (``tests/a2a_helpers.py``),
which drives the same ``create_jsonrpc_routes(..., enable_v0_3_compat=True)``
routing/dispatch/error-shaping path production uses over the harness's
``x-adcp-auth`` header contract (no middleware — the builder extracts the
token directly), seeds an owned in-memory task on the handler instance, and
asserts sibling denial matches unknown-id on the wire (code/message shape).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from tests.a2a_helpers import (
    OWNED_TASK_ID,
    OWNED_TASK_OWNER_TOK,
    OWNED_TASK_SIBLING_TOK,
    TASK_METHOD_MATRIX,
    assert_wire_task_not_found,
    build_a2a_jsonrpc_client,
    seeded_owned_a2a_handler,
    seeded_owner_sibling_resolver,
)


def _post_task(client: TestClient, *, method: str, task_id: str, token: str | None) -> dict:
    headers = {"x-adcp-auth": token} if token is not None else {}
    response = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {"id": task_id}},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("method", [row[2] for row in TASK_METHOD_MATRIX])
def test_sibling_wire_error_matches_unknown_id(method):
    """Sibling ownership miss and unknown id share wire code/message shape.

    Mutating ``!= expected_owner`` out of ``_get_owned_in_memory_task_or_raise``
    must redden this: sibling would get a result while unknown still errors.
    """
    handler = seeded_owned_a2a_handler()
    resolve = seeded_owner_sibling_resolver()

    client = build_a2a_jsonrpc_client(handler)
    with (
        patch("src.core.resolved_identity.resolve_identity", side_effect=resolve),
        patch.object(handler, "_log_a2a_operation"),
    ):
        sibling_body = _post_task(client, method=method, task_id=OWNED_TASK_ID, token=OWNED_TASK_SIBLING_TOK)
        unknown_body = _post_task(client, method=method, task_id="task_does_not_exist", token=OWNED_TASK_OWNER_TOK)
        owner_body = _post_task(client, method=method, task_id=OWNED_TASK_ID, token=OWNED_TASK_OWNER_TOK)

    assert "error" in sibling_body
    assert "error" in unknown_body
    assert "result" in owner_body

    # Exact equality — startswith would miss a suffix identity leak.
    # v0.3 compat flattens structured data to null (#1670); when that lifts,
    # the strict xfail in tests/e2e/test_a2a_endpoints_working.py XPASSes.
    assert_wire_task_not_found(sibling_body["error"], OWNED_TASK_ID)
    assert_wire_task_not_found(unknown_body["error"], "task_does_not_exist")
    assert owner_body["result"]["id"] == OWNED_TASK_ID


@pytest.mark.parametrize("method", [row[2] for row in TASK_METHOD_MATRIX])
def test_unauthenticated_wire_is_auth_failure_not_task_not_found(method):
    """No Authorization → auth-failure shape, distinct from not-found on the wire."""
    handler = seeded_owned_a2a_handler()
    client = build_a2a_jsonrpc_client(handler)
    with patch.object(handler, "_log_a2a_operation"):
        body = _post_task(client, method=method, task_id=OWNED_TASK_ID, token=None)

    assert "error" in body
    err = body["error"]
    # Compat still uses -32603; distinction from not-found is the message string.
    # See also the strict xfail sibling grading -32001 when #1670 lands.
    assert err["code"] == -32603
    assert err["data"] is None
    assert err["message"] == "Missing authentication token"

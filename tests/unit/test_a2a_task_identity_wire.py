"""In-process JSON-RPC wire grade for A2A task ownership (#1702 / #1720).

live_server xfails only POST an unknown id — they never hit the owner-compare
branch. This builds the same ``create_jsonrpc_routes(..., enable_v0_3_compat=True)``
path production uses, seeds an owned in-memory task on the handler instance, and
asserts sibling denial matches unknown-id on the wire (code/message shape).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from a2a.server.context import ServerCallContext
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.types import Task, TaskState, TaskStatus
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler, _TaskOwner
from src.core.auth_context import AUTH_CONTEXT_STATE_KEY, AuthContext
from tests.factories import PrincipalFactory

_TENANT = "tenant_a"
_OWNER = "principal_owner"
_SIBLING = "principal_sibling"
_TASK_ID = "task_owned_abc"
_OWNER_TOK = "owner-tok"
_SIBLING_TOK = "sibling-tok"


class _AuthHeaderContextBuilder(ServerCallContextBuilder):
    """Minimal builder: Authorization Bearer → AuthContext (no middleware)."""

    def build(self, request: Request) -> ServerCallContext:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return ServerCallContext(
            state={
                AUTH_CONTEXT_STATE_KEY: AuthContext(
                    auth_token=token,
                    headers={k.lower(): v for k, v in request.headers.items()},
                )
            }
        )


def _seeded_handler() -> AdCPRequestHandler:
    handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
    handler.tasks = {_TASK_ID: Task(id=_TASK_ID, status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))}
    handler._task_owners = {_TASK_ID: _TaskOwner(tenant_id=_TENANT, principal_id=_OWNER)}
    handler._task_push_configs = {}
    return handler


def _client_for(handler: AdCPRequestHandler) -> TestClient:
    routes = create_jsonrpc_routes(
        request_handler=handler,
        rpc_url="/a2a",
        context_builder=_AuthHeaderContextBuilder(),
        enable_v0_3_compat=True,
    )
    return TestClient(Starlette(routes=routes))


def _post_task(client: TestClient, *, method: str, task_id: str, token: str) -> dict:
    response = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {"id": task_id}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("method", ["tasks/get", "tasks/cancel"])
def test_sibling_wire_error_matches_unknown_id(method):
    """Sibling ownership miss and unknown id share wire code/message shape.

    Mutating ``!= expected_owner`` out of ``_get_owned_in_memory_task_or_raise``
    must redden this: sibling would get a result while unknown still errors.
    """
    handler = _seeded_handler()
    owner = PrincipalFactory.make_identity(principal_id=_OWNER, tenant_id=_TENANT, protocol="a2a")
    sibling = PrincipalFactory.make_identity(principal_id=_SIBLING, tenant_id=_TENANT, protocol="a2a")

    def resolve(*, auth_token: str | None, **_kwargs):
        if auth_token == _SIBLING_TOK:
            return sibling
        if auth_token == _OWNER_TOK:
            return owner
        raise AssertionError(f"unexpected token: {auth_token!r}")

    client = _client_for(handler)
    with (
        patch("src.core.resolved_identity.resolve_identity", side_effect=resolve),
        patch.object(handler, "_log_a2a_operation"),
    ):
        sibling_body = _post_task(client, method=method, task_id=_TASK_ID, token=_SIBLING_TOK)
        unknown_body = _post_task(client, method=method, task_id="task_does_not_exist", token=_OWNER_TOK)
        owner_body = _post_task(client, method=method, task_id=_TASK_ID, token=_OWNER_TOK)

    assert "error" in sibling_body
    assert "error" in unknown_body
    assert "result" in owner_body

    sibling_err = sibling_body["error"]
    unknown_err = unknown_body["error"]
    assert sibling_err["code"] == unknown_err["code"] == -32603
    assert sibling_err.get("data") == unknown_err.get("data")
    assert sibling_err["message"].startswith("Task not found:")
    assert unknown_err["message"].startswith("Task not found:")
    assert _TASK_ID in sibling_err["message"]
    assert "task_does_not_exist" in unknown_err["message"]
    assert owner_body["result"]["id"] == _TASK_ID

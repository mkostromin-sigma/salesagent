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
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
from src.core.auth_context import AUTH_CONTEXT_STATE_KEY, AuthContext
from tests.a2a_helpers import (
    OWNED_TASK_ID,
    OWNED_TASK_OWNER,
    OWNED_TASK_OWNER_TOK,
    OWNED_TASK_SIBLING,
    OWNED_TASK_SIBLING_TOK,
    OWNED_TASK_TENANT,
    auth_headers_mapping,
    seeded_owned_a2a_handler,
    token_identity_resolver,
)
from tests.factories import PrincipalFactory

_TENANT = OWNED_TASK_TENANT
_OWNER = OWNED_TASK_OWNER
_SIBLING = OWNED_TASK_SIBLING
_TASK_ID = OWNED_TASK_ID
_OWNER_TOK = OWNED_TASK_OWNER_TOK
_SIBLING_TOK = OWNED_TASK_SIBLING_TOK


class _AuthHeaderContextBuilder(ServerCallContextBuilder):
    """Minimal builder: Authorization Bearer → AuthContext (no middleware)."""

    def build(self, request: Request) -> ServerCallContext:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return ServerCallContext(
            state={
                AUTH_CONTEXT_STATE_KEY: AuthContext(
                    auth_token=token,
                    headers=auth_headers_mapping(dict(request.headers.items())),
                )
            }
        )


def _client_for(handler: AdCPRequestHandler) -> TestClient:
    routes = create_jsonrpc_routes(
        request_handler=handler,
        rpc_url="/a2a",
        context_builder=_AuthHeaderContextBuilder(),
        enable_v0_3_compat=True,
    )
    return TestClient(Starlette(routes=routes))


def _post_task(client: TestClient, *, method: str, task_id: str, token: str | None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    response = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {"id": task_id}},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("method", ["tasks/get", "tasks/cancel"])
def test_sibling_wire_error_matches_unknown_id(method):
    """Sibling ownership miss and unknown id share wire code/message shape.

    Mutating ``!= expected_owner`` out of ``_get_owned_in_memory_task_or_raise``
    must redden this: sibling would get a result while unknown still errors.
    """
    handler = seeded_owned_a2a_handler()
    owner = PrincipalFactory.make_identity(principal_id=_OWNER, tenant_id=_TENANT, protocol="a2a")
    sibling = PrincipalFactory.make_identity(principal_id=_SIBLING, tenant_id=_TENANT, protocol="a2a")
    resolve = token_identity_resolver({_SIBLING_TOK: sibling, _OWNER_TOK: owner})

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
    # v0.3 compat flattens structured ``data`` to null (#1670); assert both None
    # today so unequal task_id payloads do not falsely fail when flattening lifts.
    assert sibling_err["code"] == unknown_err["code"] == -32603
    assert sibling_err.get("data") is None
    assert unknown_err.get("data") is None
    assert sibling_err["message"].startswith("Task not found:")
    assert unknown_err["message"].startswith("Task not found:")
    assert _TASK_ID in sibling_err["message"]
    assert "task_does_not_exist" in unknown_err["message"]
    assert owner_body["result"]["id"] == _TASK_ID


@pytest.mark.parametrize("method", ["tasks/get", "tasks/cancel"])
def test_unauthenticated_wire_is_auth_failure_not_task_not_found(method):
    """No Authorization → auth-failure shape, distinct from not-found on the wire."""
    handler = seeded_owned_a2a_handler()
    client = _client_for(handler)
    with patch.object(handler, "_log_a2a_operation"):
        body = _post_task(client, method=method, task_id=_TASK_ID, token=None)

    assert "error" in body
    err = body["error"]
    # Compat still uses -32603; distinction from not-found is the message string.
    assert err["code"] == -32603
    assert "Task not found" not in err["message"]
    assert "Missing authentication token" in err["message"]

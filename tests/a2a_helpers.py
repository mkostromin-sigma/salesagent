"""Shared test helpers for A2A handler tests.

Provides make_a2a_context() to build a ServerCallContext the same way
AdCPCallContextBuilder.build() does in production, but without needing
a Starlette request object.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from unittest.mock import patch

from a2a.server.context import ServerCallContext
from a2a.types import Task, TaskNotFoundError, TaskState, TaskStatus

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler, _task_not_found_message, _TaskOwner
from src.core.auth_context import AUTH_CONTEXT_STATE_KEY, AuthContext
from src.core.resolved_identity import ResolvedIdentity

# Shared ownership fixtures for unit + in-process wire altitudes (#1702 / #1720).
OWNED_TASK_TENANT = "tenant_a"
OWNED_TASK_OWNER = "principal_owner"
OWNED_TASK_SIBLING = "principal_sibling"
OWNED_TASK_ID = "task_owned_abc"
OWNED_TASK_OWNER_TOK = "owner-tok"
OWNED_TASK_SIBLING_TOK = "sibling-tok"


def make_a2a_context(
    auth_token: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> ServerCallContext:
    """Build a ServerCallContext for A2A handler tests.

    Mirrors AdCPCallContextBuilder.build() — populates state["auth_context"]
    with an AuthContext containing the given token and headers.

    Args:
        auth_token: Bearer token (None for unauthenticated).
        headers: HTTP headers dict (e.g., {"host": "acme.example.com"}).

    Returns:
        ServerCallContext ready to pass to handler.on_message_send(params, context=ctx).
    """
    auth_ctx = AuthContext(
        auth_token=auth_token,
        headers=auth_headers_mapping(headers) if headers is not None else auth_headers_mapping({}),
    )
    return ServerCallContext(state={AUTH_CONTEXT_STATE_KEY: auth_ctx})


@contextmanager
def a2a_auth_as(handler: AdCPRequestHandler, identity: ResolvedIdentity) -> Iterator[None]:
    """Patch token extract + identity resolve for a single authenticated call."""
    with (
        patch.object(handler, "_get_auth_token", return_value="tok"),
        patch.object(handler, "_resolve_a2a_identity", return_value=identity),
    ):
        yield


def seeded_owned_a2a_handler(
    *,
    task_id: str = OWNED_TASK_ID,
    tenant_id: str = OWNED_TASK_TENANT,
    principal_id: str = OWNED_TASK_OWNER,
) -> AdCPRequestHandler:
    """Minimal owned in-memory task handler (bypasses ``__init__`` for unit/wire)."""
    handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
    handler.tasks = {task_id: Task(id=task_id, status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))}
    handler._task_owners = {task_id: _TaskOwner(tenant_id=tenant_id, principal_id=principal_id)}
    return handler


def token_identity_resolver(
    mapping: Mapping[str, ResolvedIdentity],
) -> Callable[..., ResolvedIdentity]:
    """``resolve_identity`` side_effect: Bearer token → identity (shared by unit+wire)."""

    def resolve(*, auth_token: str | None, **_kwargs: object) -> ResolvedIdentity:
        if auth_token not in mapping:
            raise AssertionError(f"unexpected token: {auth_token!r}")
        return mapping[auth_token]

    return resolve


def auth_headers_mapping(headers: Mapping[str, str]) -> MappingProxyType[str, str]:
    """Immutable header map for ``AuthContext`` (matches production typing)."""
    return MappingProxyType({k.lower(): v for k, v in headers.items()})


def assert_task_not_found_nondisclosure(
    exc: TaskNotFoundError,
    task_id: str,
    *,
    forbidden_substrings: tuple[str, ...] = (
        OWNED_TASK_TENANT,
        OWNED_TASK_OWNER,
        OWNED_TASK_SIBLING,
        "leaked_tenant",
    ),
) -> None:
    """Shared non-disclosure oracle for unit-altitude TaskNotFoundError objects.

    Literal message/data — never call ``_task_not_found`` for the expected side
    (both sides would move together and hide an identity leak). Message text must
    match production ``_task_not_found_message`` so telemetry and wire stay joined.
    """
    assert exc.message == _task_not_found_message(task_id)
    # Grades exception object ``data``, not the wire envelope (compat drops it).
    assert exc.data == {"task_id": task_id}
    blob = f"{exc.message}{exc.data!s}"
    for needle in forbidden_substrings:
        assert needle not in blob


def assert_wire_task_not_found(err: Mapping[str, object], task_id: str) -> None:
    """Exact wire-body oracle for not-found (v0.3 compat: code -32603, data null).

    When #1670 removes flattening, the strict xfail sibling at
    ``tests/e2e/test_a2a_endpoints_working.py`` (TestA2AServerIntegration) XPASSes
    by design while this hard-fails — keep the pointer at the failure site.
    """
    assert err == {
        "code": -32603,
        "message": _task_not_found_message(task_id),
        "data": None,
    }

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
from a2a.types import Task, TaskState, TaskStatus

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler, _TaskOwner
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
    headers: dict[str, str] | None = None,
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
    auth_ctx = AuthContext(auth_token=auth_token, headers=headers or {})
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
        if auth_token is None or auth_token not in mapping:
            raise AssertionError(f"unexpected token: {auth_token!r}")
        return mapping[auth_token]

    return resolve


def auth_headers_mapping(headers: Mapping[str, str]) -> MappingProxyType[str, str]:
    """Immutable header map for ``AuthContext`` (matches production typing)."""
    return MappingProxyType({k.lower(): v for k, v in headers.items()})

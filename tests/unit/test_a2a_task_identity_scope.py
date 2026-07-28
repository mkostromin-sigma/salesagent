"""Identity-scope gate for A2A in-memory tasks/get and tasks/cancel (#1702).

A bare ``self.tasks.get(task_id)`` served (or canceled) any caller's request
once they knew the id. These tests pin auth-first ownership against
``_task_owners`` and prove wrong-principal callers get the same
``TaskNotFoundError`` as an unknown id (no existence oracle). Auth failures
propagate as ``InvalidRequestError`` and do not collapse to not-found.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    InternalError,
    InvalidRequestError,
    Message,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskNotFoundError,
    TaskState,
    TaskStatus,
)
from sqlalchemy.exc import OperationalError

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler, _TaskOwner
from src.core.schemas import GetProductsResponse
from tests.a2a_helpers import a2a_auth_as, make_a2a_context
from tests.factories import PrincipalFactory

_TENANT = "tenant_a"
_OWNER = "principal_owner"
_SIBLING = "principal_sibling"
_OTHER_TENANT = "tenant_b"
_TASK_ID = "task_owned_abc"


def _owned_handler() -> AdCPRequestHandler:
    handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
    done = Task(id=_TASK_ID, status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
    handler.tasks = {_TASK_ID: done}
    handler._task_owners = {_TASK_ID: _TaskOwner(tenant_id=_TENANT, principal_id=_OWNER)}
    return handler


def _make_nl_message(text: str) -> SendMessageRequest:
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
    )
    message.parts.append(Part(text=text))
    return SendMessageRequest(message=message)


def _assert_not_found_matches(exc: TaskNotFoundError, task_id: str) -> None:
    """Grades the exception object (not the JSON-RPC wire envelope)."""
    expected = AdCPRequestHandler._task_not_found(task_id)
    assert exc.message == expected.message
    # Grades exception object ``data``, not the wire envelope (compat drops it).
    assert exc.data == expected.data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_cls, method_name",
    [(GetTaskRequest, "on_get_task"), (CancelTaskRequest, "on_cancel_task")],
)
async def test_create_records_owner_and_scopes_poll(request_cls, method_name):
    """Real constructor create→poll: owner allowed; sibling/other-tenant denied."""
    handler = AdCPRequestHandler()
    owner = PrincipalFactory.make_identity(principal_id=_OWNER, tenant_id=_TENANT, protocol="a2a")
    sibling = PrincipalFactory.make_identity(principal_id=_SIBLING, tenant_id=_TENANT, protocol="a2a")
    other_tenant = PrincipalFactory.make_identity(principal_id=_OWNER, tenant_id=_OTHER_TENANT, protocol="a2a")
    ctx = make_a2a_context(auth_token="test-token", headers={"host": "test.example.com"})
    params = _make_nl_message("Show me available products in the catalog")

    with patch("src.core.resolved_identity.resolve_identity", return_value=owner):
        with patch("src.a2a_server.adcp_a2a_server.core_get_products_tool") as mock_products:
            mock_products.return_value = GetProductsResponse(products=[])
            created = await handler.on_message_send(params, context=ctx)

    task_id = created.id
    assert handler._task_owners[task_id] == _TaskOwner(tenant_id=_TENANT, principal_id=_OWNER)

    with a2a_auth_as(handler, owner):
        task = await getattr(handler, method_name)(request_cls(id=task_id), context=None)
    assert task.id == task_id
    if method_name == "on_cancel_task":
        assert task.status.state == TaskState.TASK_STATE_CANCELED
    else:
        assert task.status.state == TaskState.TASK_STATE_COMPLETED

    # Re-seed completed state so deny checks do not depend on cancel mutation.
    handler.tasks[task_id].status.CopyFrom(TaskStatus(state=TaskState.TASK_STATE_COMPLETED))

    with a2a_auth_as(handler, sibling):
        with pytest.raises(TaskNotFoundError) as sibling_exc:
            await getattr(handler, method_name)(request_cls(id=task_id), context=None)

    with a2a_auth_as(handler, other_tenant):
        with pytest.raises(TaskNotFoundError) as other_exc:
            await getattr(handler, method_name)(request_cls(id=task_id), context=None)

    with a2a_auth_as(handler, owner):
        with pytest.raises(TaskNotFoundError) as unknown_exc:
            await getattr(handler, method_name)(request_cls(id="task_does_not_exist"), context=None)

    _assert_not_found_matches(sibling_exc.value, task_id)
    _assert_not_found_matches(other_exc.value, task_id)
    _assert_not_found_matches(unknown_exc.value, "task_does_not_exist")
    # Deny shape for the owned id matches the canonical not-found from this handler.
    assert sibling_exc.value.message == other_exc.value.message
    assert sibling_exc.value.data == other_exc.value.data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_cls, method_name",
    [(GetTaskRequest, "on_get_task"), (CancelTaskRequest, "on_cancel_task")],
)
async def test_owner_can_access_owned_in_memory_task(request_cls, method_name):
    """The recorded owner authenticates and is served / can cancel."""
    handler = _owned_handler()
    identity = PrincipalFactory.make_identity(principal_id=_OWNER, tenant_id=_TENANT, protocol="a2a")

    with a2a_auth_as(handler, identity):
        task = await getattr(handler, method_name)(request_cls(id=_TASK_ID), context=None)

    assert task.id == _TASK_ID
    if method_name == "on_cancel_task":
        assert task.status.state == TaskState.TASK_STATE_CANCELED
    else:
        assert task.status.state == TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_cls, method_name",
    [(GetTaskRequest, "on_get_task"), (CancelTaskRequest, "on_cancel_task")],
)
async def test_sibling_principal_denied_same_as_unknown(request_cls, method_name):
    """Same-tenant sibling must not read or cancel — identical to unknown id."""
    handler = _owned_handler()
    sibling = PrincipalFactory.make_identity(principal_id=_SIBLING, tenant_id=_TENANT, protocol="a2a")
    owner = PrincipalFactory.make_identity(principal_id=_OWNER, tenant_id=_TENANT, protocol="a2a")

    with a2a_auth_as(handler, sibling):
        with pytest.raises(TaskNotFoundError) as deny_exc:
            await getattr(handler, method_name)(request_cls(id=_TASK_ID), context=None)

    with a2a_auth_as(handler, owner):
        with pytest.raises(TaskNotFoundError) as unknown_exc:
            await getattr(handler, method_name)(request_cls(id="task_does_not_exist"), context=None)

    _assert_not_found_matches(deny_exc.value, _TASK_ID)
    _assert_not_found_matches(unknown_exc.value, "task_does_not_exist")
    # Sibling denial must not mutate cancel state.
    assert handler.tasks[_TASK_ID].status.state == TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_cls, method_name, wire_message",
    [
        (GetTaskRequest, "on_get_task", "get task failed"),
        (CancelTaskRequest, "on_cancel_task", "cancel task failed"),
    ],
)
async def test_auth_infra_failure_is_internal_error_not_task_not_found(request_cls, method_name, wire_message):
    """DB/infra failure during identity resolve must not collapse to TaskNotFoundError.

    Mutating the ``_authenticate`` except branch back to ``_task_not_found`` must
    redden this test: buyers see a fixed human-phrase InternalError, not not-found.
    """
    handler = _owned_handler()

    with (
        patch.object(handler, "_get_auth_token", return_value="tok"),
        patch.object(
            handler,
            "_resolve_a2a_identity",
            side_effect=OperationalError("db down", None, None),
        ),
    ):
        with pytest.raises(InternalError) as exc_info:
            await getattr(handler, method_name)(request_cls(id=_TASK_ID), context=None)

    raised = exc_info.value
    assert not isinstance(raised, TaskNotFoundError)
    assert raised.message == wire_message
    assert "_" not in raised.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_cls, method_name",
    [(GetTaskRequest, "on_get_task"), (CancelTaskRequest, "on_cancel_task")],
)
async def test_sibling_denied_via_real_auth_token_path(request_cls, method_name):
    """Ownership compare with real ``_get_auth_token`` (only resolve_identity patched).

    Unlike ``a2a_auth_as`` tests, the token is extracted from ServerCallContext so
    mutating ``!= expected_owner`` out of the gate reddens this path — unknown-id
    alone would stay green.
    """
    handler = _owned_handler()
    owner = PrincipalFactory.make_identity(principal_id=_OWNER, tenant_id=_TENANT, protocol="a2a")
    sibling = PrincipalFactory.make_identity(principal_id=_SIBLING, tenant_id=_TENANT, protocol="a2a")

    def resolve(*, auth_token: str | None, **_kwargs):
        if auth_token == "sibling-tok":
            return sibling
        if auth_token == "owner-tok":
            return owner
        raise AssertionError(f"unexpected token: {auth_token!r}")

    with patch("src.core.resolved_identity.resolve_identity", side_effect=resolve):
        sibling_ctx = make_a2a_context(auth_token="sibling-tok", headers={"host": "test.example.com"})
        with pytest.raises(TaskNotFoundError) as deny_exc:
            await getattr(handler, method_name)(request_cls(id=_TASK_ID), context=sibling_ctx)

        owner_ctx = make_a2a_context(auth_token="owner-tok", headers={"host": "test.example.com"})
        with pytest.raises(TaskNotFoundError) as unknown_exc:
            await getattr(handler, method_name)(request_cls(id="task_does_not_exist"), context=owner_ctx)

    _assert_not_found_matches(deny_exc.value, _TASK_ID)
    _assert_not_found_matches(unknown_exc.value, "task_does_not_exist")
    assert handler.tasks[_TASK_ID].status.state == TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_cls, method_name",
    [(GetTaskRequest, "on_get_task"), (CancelTaskRequest, "on_cancel_task")],
)
async def test_unauthenticated_poller_raises_invalid_request(request_cls, method_name):
    """Missing token raises InvalidRequestError — must not collapse to TaskNotFoundError."""
    handler = _owned_handler()

    with patch.object(handler, "_get_auth_token", return_value=None):
        with pytest.raises(InvalidRequestError):
            await getattr(handler, method_name)(request_cls(id=_TASK_ID), context=None)

    assert handler.tasks[_TASK_ID].status.state == TaskState.TASK_STATE_COMPLETED


def test_resolve_identity_without_principal_id_raises_invalid_request():
    """Authenticated resolve with no principal_id hits the :290-291 guard."""
    handler = AdCPRequestHandler()
    no_principal = PrincipalFactory.make_identity(principal_id=None, tenant_id=_TENANT, protocol="a2a")
    ctx = make_a2a_context(auth_token="tok", headers={"host": "test.example.com"})

    with patch("src.core.resolved_identity.resolve_identity", return_value=no_principal):
        with pytest.raises(InvalidRequestError, match="invalid or expired"):
            handler._resolve_a2a_identity("tok", require_valid_token=True, context=ctx)


@pytest.mark.asyncio
async def test_discovery_create_records_anonymous_owner_without_auth():
    """Unauthenticated discovery records owner from ResolvedIdentity (never None).

    Regression for Integration infra: mocking ``_resolve_a2a_identity`` to return
    ``None`` passed before create+own co-location, then AttributeError on
    ``identity.tenant_id``. Production always returns ResolvedIdentity.
    """
    from src.core.exceptions import AdCPValidationError
    from tests.utils.a2a_helpers import create_a2a_message_with_skill

    handler = AdCPRequestHandler()
    anonymous = PrincipalFactory.make_anonymous_a2a_identity(tenant_id=_TENANT)

    async def raise_validation(params, identity):
        raise AdCPValidationError("synthetic discovery failure")

    with patch.object(handler, "_handle_get_products_skill", raise_validation):
        handler._get_auth_token = lambda context=None: None
        handler._resolve_a2a_identity = lambda *args, **kwargs: anonymous
        created = await handler.on_message_send(
            SendMessageRequest(message=create_a2a_message_with_skill("get_products", {"brief": "test"})),
            context=make_a2a_context(auth_token=None),
        )

    assert created.id in handler._task_owners
    assert handler._task_owners[created.id] == _TaskOwner(tenant_id=_TENANT, principal_id=None)
    assert created.id in handler.tasks

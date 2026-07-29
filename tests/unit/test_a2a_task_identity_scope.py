"""Identity-scope gate for A2A in-memory tasks/get and tasks/cancel (#1702).

A bare ``self.tasks.get(task_id)`` served (or canceled) any caller's request
once they knew the id. These tests pin auth-first ownership against
``_task_owners`` and prove wrong-principal callers get the same
``TaskNotFoundError`` as an unknown id (no existence oracle). Auth failures
propagate as ``InvalidRequestError`` and do not collapse to not-found.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    InternalError,
    InvalidRequestError,
    SendMessageRequest,
    TaskNotFoundError,
    TaskState,
    TaskStatus,
)
from sqlalchemy.exc import OperationalError

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler, _TaskOwner
from src.core.exceptions import AdCPTaskNotFoundError
from src.core.schemas import GetProductsResponse
from tests.a2a_helpers import (
    OWNED_TASK_ID,
    OWNED_TASK_OWNER,
    OWNED_TASK_OWNER_TOK,
    OWNED_TASK_SIBLING,
    OWNED_TASK_SIBLING_TOK,
    OWNED_TASK_TENANT,
    a2a_auth_as,
    make_a2a_context,
    seeded_owned_a2a_handler,
    token_identity_resolver,
)
from tests.factories import PrincipalFactory
from tests.utils.a2a_helpers import create_a2a_text_message

_TENANT = OWNED_TASK_TENANT
_OWNER = OWNED_TASK_OWNER
_SIBLING = OWNED_TASK_SIBLING
_OTHER_TENANT = "tenant_b"
_TASK_ID = OWNED_TASK_ID

# Op ids passed to ``_authenticate`` — wire phrases must stay underscore-free.
_AUTH_OPERATION_IDS = (
    "get_task",
    "cancel_task",
    "get_push_notification_config",
    "set_push_notification_config",
    "list_push_notification_configs",
    "delete_push_notification_config",
)


def _assert_not_found_matches(exc: TaskNotFoundError, task_id: str) -> None:
    """Grades the exception object (not the JSON-RPC wire envelope).

    Literal message/data — never call ``_task_not_found`` for the expected side
    (both sides would move together and hide an identity leak in the envelope).
    """
    assert exc.message == f"Task not found: {task_id}"
    # Grades exception object ``data``, not the wire envelope (compat drops it).
    assert exc.data == {"task_id": task_id}
    blob = f"{exc.message}{exc.data!s}"
    assert _TENANT not in blob
    assert _OWNER not in blob
    assert _SIBLING not in blob
    assert "leaked_tenant" not in blob


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
    params = SendMessageRequest(message=create_a2a_text_message("Show me available products in the catalog"))

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
    handler = seeded_owned_a2a_handler()
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
    handler = seeded_owned_a2a_handler()
    sibling = PrincipalFactory.make_identity(principal_id=_SIBLING, tenant_id=_TENANT, protocol="a2a")
    owner = PrincipalFactory.make_identity(principal_id=_OWNER, tenant_id=_TENANT, protocol="a2a")

    with a2a_auth_as(handler, sibling):
        with patch("src.a2a_server.adcp_a2a_server.record_boundary_error") as record_error:
            with pytest.raises(TaskNotFoundError) as deny_exc:
                await getattr(handler, method_name)(request_cls(id=_TASK_ID), context=None)

    record_error.assert_called_once()
    call_kwargs = record_error.call_args
    assert call_kwargs.args[0] == "a2a"
    assert call_kwargs.args[1] in {"get_task", "cancel_task"}
    telem = call_kwargs.args[2]
    assert isinstance(telem, AdCPTaskNotFoundError)
    assert not isinstance(telem, TaskNotFoundError)
    assert call_kwargs.kwargs["tenant_id"] == _TENANT
    assert call_kwargs.kwargs["principal_id"] == _SIBLING

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
    handler = seeded_owned_a2a_handler()

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
    assert type(raised) is InternalError
    assert raised.message == wire_message
    assert "_" not in raised.message
    # Recovery envelope attached (compat may still flatten ``data`` on the wire).
    assert raised.data is not None
    assert "adcp_error" in raised.data


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
    handler = seeded_owned_a2a_handler()
    owner = PrincipalFactory.make_identity(principal_id=_OWNER, tenant_id=_TENANT, protocol="a2a")
    sibling = PrincipalFactory.make_identity(principal_id=_SIBLING, tenant_id=_TENANT, protocol="a2a")
    resolve = token_identity_resolver(
        {
            OWNED_TASK_SIBLING_TOK: sibling,
            OWNED_TASK_OWNER_TOK: owner,
        }
    )

    with patch("src.core.resolved_identity.resolve_identity", side_effect=resolve):
        sibling_ctx = make_a2a_context(auth_token=OWNED_TASK_SIBLING_TOK, headers={"host": "test.example.com"})
        with pytest.raises(TaskNotFoundError) as deny_exc:
            await getattr(handler, method_name)(request_cls(id=_TASK_ID), context=sibling_ctx)

        owner_ctx = make_a2a_context(auth_token=OWNED_TASK_OWNER_TOK, headers={"host": "test.example.com"})
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
    handler = seeded_owned_a2a_handler()

    with patch.object(handler, "_get_auth_token", return_value=None):
        with pytest.raises(InvalidRequestError):
            await getattr(handler, method_name)(request_cls(id=_TASK_ID), context=None)

    assert handler.tasks[_TASK_ID].status.state == TaskState.TASK_STATE_COMPLETED


def test_resolve_identity_without_principal_id_raises_invalid_request():
    """Authenticated resolve with no principal_id hits the no-principal guard."""
    handler = AdCPRequestHandler()
    no_principal = PrincipalFactory.make_identity(principal_id=None, tenant_id=_TENANT, protocol="a2a")
    ctx = make_a2a_context(auth_token="tok", headers={"host": "test.example.com"})

    with patch("src.core.resolved_identity.resolve_identity", return_value=no_principal):
        with pytest.raises(InvalidRequestError, match="invalid or expired"):
            handler._resolve_a2a_identity("tok", require_valid_token=True, context=ctx)


def test_auth_operation_ids_are_underscore_replaceable():
    """Op ids passed to ``_authenticate`` must not need a hand-maintained phrase map."""
    for op_id in _AUTH_OPERATION_IDS:
        phrase = op_id.replace("_", " ")
        assert "_" not in phrase
        assert phrase == " ".join(op_id.split("_"))


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

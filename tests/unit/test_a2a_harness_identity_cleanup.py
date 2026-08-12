"""Harness-contract: token-mode prepare drops unit-mode identity lambdas (#1780)."""

from __future__ import annotations

from tests.factories.principal import PrincipalFactory
from tests.harness._base import BaseTestEnv


class _A2APrepareEnv(BaseTestEnv):
    """Minimal env so ``_prepare_a2a_server_context`` is reachable without a domain."""

    EXTERNAL_PATCHES: dict[str, str] = {}

    def call_impl(self, **kwargs: object) -> None:
        raise NotImplementedError


def test_token_mode_prepare_clears_unit_mode_identity_lambdas() -> None:
    """Deleting the pops in token-mode prepare must redden this oracle.

    Sequence: unit-mode prepare installs identity lambdas on the shared
    handler; token-mode prepare must drop them so a later real-token dispatch
    cannot silently reuse the previous caller's identity.

    Also asserts no callable instance-dict shadows remain over class methods —
    a third injected attribute (e.g. ``_authenticate``) must redden even when
    the two named pops stay green.
    """
    with _A2APrepareEnv(use_real_db=False) as env:
        handler = env.a2a_handler
        unit_identity = PrincipalFactory.make_identity(
            principal_id="unit_principal",
            tenant_id="unit_tenant",
            protocol="a2a",
            auth_token=None,
        )
        token_identity = PrincipalFactory.make_identity(
            principal_id="token_principal",
            tenant_id="token_tenant",
            protocol="a2a",
            auth_token="harness-contract-tok",
        )

        env._prepare_a2a_server_context(handler, unit_identity)
        assert "_resolve_a2a_identity" in handler.__dict__
        assert "_get_auth_token" in handler.__dict__

        env._prepare_a2a_server_context(handler, token_identity)
        assert "_resolve_a2a_identity" not in handler.__dict__
        assert "_get_auth_token" not in handler.__dict__
        # Any instance-dict callable that shadows a class method defeats the gate.
        assert {k for k in handler.__dict__ if callable(getattr(type(handler), k, None))} == set()


def test_a2a_task_slots_are_distinct_typed_attributes() -> None:
    """Protobuf and wire Task slots must stay separate attributes (wrong-class write cannot hide)."""
    with _A2APrepareEnv(use_real_db=False) as env:
        assert hasattr(env, "_last_a2a_task")
        assert hasattr(env, "_last_a2a_wire_task")
        assert env._last_a2a_task is None
        assert env._last_a2a_wire_task is None
        # Distinct storage: writing one must not populate the other.
        sentinel = object()
        env._last_a2a_wire_task = sentinel  # type: ignore[assignment]
        assert env._last_a2a_task is None
        assert env.last_a2a_wire_task is sentinel

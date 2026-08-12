"""Regression: REST auth deps extract testing context from headers (#1830).

Parity with A2A ``_resolve_a2a_identity`` — without ``AdCPTestContext.from_headers``,
``X-Mock-Time`` / ``X-Dry-Run`` never reach ``get_media_buys`` over e2e_rest.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.auth_context import AuthContext, _require_auth_dep, _resolve_auth_dep
from src.core.schemas import GetMediaBuysRequest
from src.core.testing_hooks import AdCPTestContext
from src.core.tools.media_buy_list import _get_media_buys_impl
from tests.factories.principal import PrincipalFactory


class TestRestTestingContextExtraction:
    """REST ``_resolve_auth_dep`` / ``_require_auth_dep`` pass testing_context."""

    def test_resolve_auth_passes_mock_time_from_headers(self):
        auth_ctx = AuthContext(
            auth_token="test-token",
            headers={
                "x-adcp-auth": "test-token",
                "x-mock-time": "2026-03-15T12:00:00Z",
            },
        )
        mock_identity = PrincipalFactory.make_identity(protocol="rest")
        with patch("src.core.resolved_identity.resolve_identity", return_value=mock_identity) as mock_resolve:
            _resolve_auth_dep(auth_ctx)

        testing_ctx = mock_resolve.call_args.kwargs.get("testing_context")
        assert testing_ctx is not None
        assert testing_ctx.mock_time == datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)

    def test_require_auth_passes_dry_run_from_headers(self):
        auth_ctx = AuthContext(
            auth_token="test-token",
            headers={
                "x-adcp-auth": "test-token",
                "x-dry-run": "true",
            },
        )
        mock_identity = PrincipalFactory.make_identity(protocol="rest")
        with patch("src.core.resolved_identity.resolve_identity", return_value=mock_identity) as mock_resolve:
            _require_auth_dep(auth_ctx)

        testing_ctx = mock_resolve.call_args.kwargs.get("testing_context")
        assert testing_ctx is not None
        assert testing_ctx.dry_run is True

    def test_resolve_auth_passes_none_without_test_headers(self):
        auth_ctx = AuthContext(
            auth_token="test-token",
            headers={"x-adcp-auth": "test-token"},
        )
        mock_identity = PrincipalFactory.make_identity(protocol="rest")
        with patch("src.core.resolved_identity.resolve_identity", return_value=mock_identity) as mock_resolve:
            _resolve_auth_dep(auth_ctx)

        assert mock_resolve.call_args.kwargs.get("testing_context") is None


class TestMediaBuyListHonorsMockTime:
    """``_get_media_buys_impl`` uses testing_context.mock_time for ``today``."""

    def test_list_today_uses_mock_time_date(self):
        mock_time = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        identity = PrincipalFactory.make_identity(
            protocol="rest",
            testing_context=AdCPTestContext(mock_time=mock_time),
        )
        captured: dict[str, object] = {}

        def _capture_fetch(_req, _principal_id, _uow, today):
            captured["today"] = today
            return []

        with (
            patch("src.core.tools.media_buy_list.MediaBuyUoW") as m_uow,
            patch("src.core.tools.media_buy_list.get_principal_object", return_value=MagicMock()),
            patch("src.core.tools.media_buy_list.require_tenant", return_value={"tenant_id": "test_tenant"}),
            patch("src.core.tools.media_buy_list._fetch_target_media_buys", side_effect=_capture_fetch),
            patch("src.core.tools.media_buy_list._fetch_creative_approvals", return_value={}),
            patch("src.core.tools.media_buy_list._fetch_packages", return_value={}),
        ):
            uow = MagicMock()
            uow.media_buys = MagicMock()
            uow.session = MagicMock()
            m_uow.return_value.__enter__.return_value = uow
            m_uow.return_value.__exit__.return_value = False

            _get_media_buys_impl(GetMediaBuysRequest(), identity=identity)

        assert captured["today"] == date(2026, 3, 15)


class TestRestE2EDispatcherMockTimeHeader:
    """RestE2EDispatcher forwards X-Mock-Time from identity / env."""

    def test_dispatcher_sets_x_mock_time_header(self):
        from tests.harness.dispatchers import RestE2EDispatcher

        mock_time = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        identity = PrincipalFactory.make_identity(
            protocol="rest",
            testing_context=AdCPTestContext(mock_time=mock_time, dry_run=True),
            auth_token="tok",
        )
        env = SimpleNamespace(
            e2e_config=SimpleNamespace(base_url="http://example.test"),
            REST_ENDPOINT="/api/v1/media-buys/query",
            REST_METHOD="post",
            build_rest_body=lambda **_kw: {},
            parse_rest_response=lambda data: data,
            parse_rest_error=lambda *_a: Exception("err"),
            _mock_time=None,
        )
        captured_headers: dict[str, str] = {}

        class _FakeResponse:
            status_code = 200
            headers = {"content-type": "application/json"}

            def json(self):
                return {"media_buys": []}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, endpoint, json=None, headers=None):  # noqa: A002
                captured_headers.update(headers or {})
                return _FakeResponse()

        with patch("httpx.Client", _FakeClient):
            RestE2EDispatcher().dispatch(env, identity=identity)

        assert captured_headers.get("x-mock-time") == "2026-03-15T12:00:00Z"
        assert captured_headers.get("x-dry-run") == "true"

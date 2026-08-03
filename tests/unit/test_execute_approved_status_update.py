"""Unit test: execute_approved_media_buy persists flight-window status after adapter success.

Bug: salesagent-mckm — execute returned (True, None) without updating ORM status.
#1696: must use flight-window status (active/scheduled/completed), not hardcode active.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.core.database.models import PersistedMediaBuyStatus
from src.core.database.repositories.creative import CreativeAssignmentRepository
from src.core.schemas import CreateMediaBuySuccess, Principal
from src.core.tools.media_buy_create import ApprovalOutcome

# Who approved, and when. Passed in by the caller and written by the same
# ``update_status`` call as the status, so the assertion can name all three.
_APPROVED_BY = "approver@example.com"
_APPROVED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _make_mock_media_buy(*, start_offset_days: int = 0):
    """Build a mock MediaBuy ORM object with minimal fields for execute_approved_media_buy."""
    now = datetime.now(UTC)
    start = now + timedelta(days=start_offset_days)
    end = start + timedelta(days=7)
    mb = MagicMock()
    mb.media_buy_id = "mb_test_001"
    mb.tenant_id = "tenant_1"
    mb.principal_id = "principal_1"
    mb.status = "pending_approval"
    mb.order_name = "Test Order"
    mb.advertiser_name = "Test Advertiser"
    mb.start_date = start.date()
    mb.end_date = end.date()
    mb.start_time = start
    mb.end_time = end
    mb.budget = Decimal("5000.00")
    mb.currency = "USD"
    mb.raw_request = {
        "brand": {"domain": "testbrand.com"},
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "packages": [{"product_id": "prod_1", "pricing_option_id": "po_1", "budget": 5000.0}],
    }
    return mb


def _make_mock_tenant():
    """Build a mock Tenant ORM object."""
    tenant = MagicMock()
    tenant.tenant_id = "tenant_1"
    tenant.name = "Test Tenant"
    tenant.subdomain = "test"
    tenant.ad_server = "mock"
    tenant.virtual_host = None
    return tenant


def _make_mock_package():
    """Build a mock MediaPackage DB object."""
    pkg = MagicMock()
    pkg.package_id = "pkg_001"
    pkg.media_buy_id = "mb_test_001"
    pkg.package_config = {"product_id": "prod_1", "name": "Test Package", "budget": 5000.0, "pricing_model": "CPM"}
    return pkg


def _make_mock_product():
    """Build a mock Product ORM object."""
    product = MagicMock()
    product.product_id = "prod_1"
    product.name = "Test Product"
    product.delivery_type = "non_guaranteed"
    product.format_ids = [{"agent_url": "https://example.com/formats", "format_id": "fmt_1", "id": "fmt_1"}]

    # Set up pricing option
    pricing_option = MagicMock()
    pricing_option.pricing_model = "CPM"
    pricing_option.rate = Decimal("10.00")
    pricing_option.currency = "USD"
    pricing_option.is_fixed = True
    pricing_option.root = pricing_option  # Self-reference for getattr(po, "root", po)
    product.pricing_options = [pricing_option]

    return product


def _run_execute_approved(media_buy, *, tenant=None, db_package=None, product=None):
    """Drive execute_approved_media_buy with standard mocks; return (success, error, status_repo)."""
    tenant = tenant or _make_mock_tenant()
    db_package = db_package or _make_mock_package()
    product = product or _make_mock_product()

    principal = Principal(
        principal_id="principal_1",
        name="Test Principal",
        platform_mappings={},
    )

    adapter_response = CreateMediaBuySuccess(
        media_buy_id="mb_test_001",
        packages=[],
    )

    mock_adapter = MagicMock()
    mock_adapter.orders_manager = None

    mock_session_1 = MagicMock()
    mock_session_2 = MagicMock()
    mock_session_3 = MagicMock()

    session_1_scalars = [
        MagicMock(first=MagicMock(return_value=tenant)),
        MagicMock(first=MagicMock(return_value=media_buy)),
        MagicMock(all=MagicMock(return_value=[db_package])),
        MagicMock(first=MagicMock(return_value=product)),
    ]
    mock_session_1.scalars = MagicMock(side_effect=session_1_scalars)
    mock_session_2.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    mock_uow_1 = MagicMock()
    mock_uow_1.__enter__ = MagicMock(return_value=mock_uow_1)
    mock_uow_1.__exit__ = MagicMock(return_value=None)
    mock_uow_1.session = mock_session_1
    mock_uow_1.media_buys = MagicMock()

    mock_repo_plids = MagicMock()
    mock_repo_plids.get_packages.return_value = [db_package]
    mock_uow_plids = MagicMock()
    mock_uow_plids.__enter__ = MagicMock(return_value=mock_uow_plids)
    mock_uow_plids.__exit__ = MagicMock(return_value=None)
    mock_uow_plids.media_buys = mock_repo_plids

    mock_uow_2 = MagicMock()
    mock_uow_2.__enter__ = MagicMock(return_value=mock_uow_2)
    mock_uow_2.__exit__ = MagicMock(return_value=None)
    mock_uow_2.session = mock_session_2
    mock_uow_2.media_buys = MagicMock()

    mock_repo_3 = MagicMock()
    mock_repo_3.get_by_id.return_value = media_buy
    mock_uow_3 = MagicMock()
    mock_uow_3.__enter__ = MagicMock(return_value=mock_uow_3)
    mock_uow_3.__exit__ = MagicMock(return_value=None)
    mock_uow_3.session = mock_session_3
    mock_uow_3.media_buys = mock_repo_3

    uow_iter = iter([mock_uow_1, mock_uow_plids, mock_uow_2, mock_uow_3])

    with (
        patch("src.core.database.repositories.MediaBuyUoW", side_effect=lambda _: next(uow_iter)),
        patch("src.core.config_loader.set_current_tenant"),
        patch(
            "src.core.config_loader.get_tenant_by_id",
            return_value={"tenant_id": "tenant_1", "adapter_type": "mock"},
        ),
        patch("src.core.auth.get_principal_object", return_value=principal),
        patch(
            "src.core.tools.media_buy_create._execute_adapter_media_buy_creation",
            return_value=adapter_response,
        ),
        patch("src.core.tools.media_buy_create._validate_creatives_before_adapter_call"),
        patch("src.core.helpers.adapter_helpers.get_adapter", return_value=mock_adapter),
    ):
        from src.core.tools.media_buy_create import execute_approved_media_buy

        success, error = execute_approved_media_buy("mb_test_001", "tenant_1")

    return success, error, mock_repo_3


class TestExecuteApprovedStatusUpdate:
    """execute_approved_media_buy must persist flight-window status after adapter success."""

    def test_status_updated_to_active_after_adapter_success(self):
        """In-flight start → ORM status 'active' (salesagent-mckm + UC-002:437)."""
        success, error, mock_repo_3 = _run_execute_approved(_make_mock_media_buy(start_offset_days=0))

        assert success is True, f"Expected success but got error: {error}"
        assert error is None
        mock_repo_3.update_status.assert_called_once_with("mb_test_001", "active")

    def test_status_updated_to_scheduled_for_future_start(self):
        """Future-start buy → ORM status 'scheduled' (must not clobber ready-arm #1696)."""
        success, error, mock_repo_3 = _run_execute_approved(_make_mock_media_buy(start_offset_days=7))

        assert success is True, f"Expected success but got error: {error}"
        assert error is None
        mock_repo_3.update_status.assert_called_once_with("mb_test_001", "scheduled")

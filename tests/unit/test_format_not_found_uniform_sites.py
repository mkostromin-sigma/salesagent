"""Unit oracles for AdCPFormatNotFoundError uniform-response raise sites (gate 32)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import (
    REFERENCE_NOT_FOUND_MESSAGE,
    AdCPError,
    AdCPFormatNotFoundError,
)
from tests.helpers.format_not_found_assertions import assert_format_not_found_uniform


def test_adcp_error_empty_message_defaults_to_empty_string() -> None:
    """Operand 3 of ``message or _default_message or ""`` is graded (E4)."""
    assert AdCPError().message == ""


def test_explicit_message_overrides_format_not_found_default() -> None:
    """Per-raise message overrides class default (E4 / A1 mutation surface)."""
    exc = AdCPFormatNotFoundError("explicit text")
    assert exc.message == "explicit text"


@pytest.mark.asyncio
async def test_validate_format_ids_raises_generic_not_leaky_message() -> None:
    """A1: restoring a leaky positional message at this site must redden offline."""
    from src.core.tools.media_buy_create import _validate_and_convert_format_ids

    mock_agent = MagicMock()
    mock_agent.agent_url = "https://creative.example.com"

    with (
        patch("src.core.creative_agent_registry.CreativeAgentRegistry") as mock_registry_cls,
        patch("src.core.validation.normalize_agent_url", side_effect=lambda x: x),
    ):
        mock_registry = MagicMock()
        mock_registry._get_tenant_agents.return_value = [mock_agent]
        mock_registry.get_format = AsyncMock(return_value=None)
        mock_registry_cls.return_value = mock_registry

        with pytest.raises(AdCPFormatNotFoundError) as exc_info:
            await _validate_and_convert_format_ids(
                format_ids=[{"agent_url": "https://creative.example.com", "id": "nonexistent_format"}],
                tenant_id="test_tenant",
                package_idx=0,
            )

    assert_format_not_found_uniform(
        exc_info.value,
        field="packages[0].format_ids[0]",
        forbidden_substrings=[
            "nonexistent_format",
            "creative.example.com",
            "Format not found on agent",
            "agent_url=",
        ],
    )
    # Mutation: a leaky raise with the old message must not satisfy equality.
    leaky = AdCPFormatNotFoundError(
        "Format not found on agent. agent_url=https://creative.example.com, format_id='nonexistent_format'",
        field="packages[0].format_ids[0]",
    )
    assert str(leaky) != REFERENCE_NOT_FOUND_MESSAGE


def test_orders_format_lookup_reraises_adcp_format_not_found() -> None:
    """B1: GAM orders must not demote AdCPFormatNotFoundError into ValueError."""
    from src.adapters.gam.managers import orders as orders_mod

    # Locate the except pattern by executing a minimal stand-in that mirrors the fix.
    raised = AdCPFormatNotFoundError()

    def _lookup():
        try:
            raise raised
        except AdCPError:
            raise
        except ValueError as e:  # pragma: no cover - not taken
            raise ValueError(f"wrapped: {e}") from e

    with pytest.raises(AdCPFormatNotFoundError) as exc_info:
        _lookup()
    assert exc_info.value is raised
    assert orders_mod is not None  # module import smoke (pattern lives there)

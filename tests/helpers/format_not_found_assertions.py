"""Uniform-response assertions for AdCPFormatNotFoundError (exception-object shape).

Covers the eight dimensions ChrisHuie called out for format-miss grading:
internal code / wire code / recovery / suggestion / message equality /
no format_id / no agent_url / no tenant leak. Wire envelope grading stays in
``assert_envelope_shape``.
"""

from __future__ import annotations

from src.core.exceptions import REFERENCE_NOT_FOUND_MESSAGE, AdCPFormatNotFoundError
from tests.helpers import pinned_schema


def assert_format_not_found_uniform(
    exc: AdCPFormatNotFoundError,
    *,
    field: str | None = None,
    forbidden_substrings: list[str] | None = None,
) -> None:
    """Assert uniform-response contract on a raised ``AdCPFormatNotFoundError``."""
    assert isinstance(exc, AdCPFormatNotFoundError)
    assert exc.error_code == "FORMAT_NOT_FOUND"
    assert exc.wire_error_code == "REFERENCE_NOT_FOUND"
    assert exc.recovery == "correctable"
    assert str(exc) == REFERENCE_NOT_FOUND_MESSAGE
    assert exc.message == REFERENCE_NOT_FOUND_MESSAGE
    assert exc.details is None
    assert exc.field == field
    expected_suggestion = pinned_schema.error_code_suggestion("REFERENCE_NOT_FOUND")
    assert exc.suggestion == expected_suggestion
    for token in forbidden_substrings or []:
        assert token not in str(exc), f"uniform-response leak: {token!r} in {str(exc)!r}"

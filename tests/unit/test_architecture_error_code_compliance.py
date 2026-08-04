"""Guard: all wire error codes must be in ``_ALLOWED_CODES`` or seller-specific shape.

AST-scans Error(code=...) construction sites in src/core/tools/ and
src/adapters/ to verify every string-literal error code is in the union of
WIRE_STANDARD_CODES (SDK STANDARD_ERROR_CODES + pinned-spec supplement),
INTERNAL_CODES, ``_SPEC_CODES``, or matches the seller-specific error code
shape (``_is_seller_specific_code``).

Also verifies that AdCPError subclass error_code class attributes are
standard or internal (complementing test_error_code_mapping.py).

Ref: GH #1248
"""

import ast
import logging
import re
from pathlib import Path

import pytest

from src.core.exceptions import INTERNAL_CODES, WIRE_STANDARD_CODES

logger = logging.getLogger(__name__)

# Spec-required codes not yet in SDK STANDARD_ERROR_CODES.
# These are mandated by AdCP BDD feature files but the SDK hasn't added them yet.
_SPEC_CODES = {
    "BILLING_NOT_SUPPORTED",  # BR-UC-011 BR-RULE-059: unsupported billing model
}

# AdCP 3.1.1 transport-errors.mdx § Seller-Specific Error Codes (MUST):
# non-standard codes MUST match X_{VENDOR}_{CODE}, VENDOR ~ [A-Z][A-Z0-9]{1,19},
# CODE ~ [A-Z][A-Z0-9_]{1,39}. A bare membership set (the prior design) merely
# allowlists a value instead of enforcing this shape, so it stays green for any
# string added to it — including one that collides with the standard
# CREATIVE_* namespace (the exact bug this guard exists to catch, GH #1835).
# A predicate rejects that by construction: only a string matching the pinned
# shape can ever pass, regardless of what gets added at a call site.
_SELLER_SPECIFIC_CODE_PATTERN = re.compile(r"^X_[A-Z][A-Z0-9]{1,19}_[A-Z][A-Z0-9_]{1,39}$")


def _is_seller_specific_code(code: str) -> bool:
    """True when *code* matches the AdCP seller-specific error code shape.

    transport-errors.mdx § Seller-Specific Error Codes (AdCP 3.1.1): sellers
    MAY emit codes outside the standard vocabulary, but any such code MUST
    match ``X_{VENDOR}_{CODE}``. Enforcing the shape (rather than allowlisting
    specific values) means a future platform code either conforms by
    construction or fails this guard — it cannot silently fork the vocabulary
    by landing a bare string in a membership set.
    """
    return bool(_SELLER_SPECIFIC_CODE_PATTERN.match(code))


# All acceptable codes: wire-standard (SDK + spec supplement) + justified
# internal + spec-required literals. Seller-specific (X_{VENDOR}_{CODE})
# codes are validated separately via _is_seller_specific_code, not by
# membership, so _ALLOWED_CODES stays a closed, auditable set.
_ALLOWED_CODES = set(WIRE_STANDARD_CODES) | INTERNAL_CODES | _SPEC_CODES


def _code_is_compliant(code: str) -> bool:
    """True when *code* is wire-standard/internal/spec, or seller-specific-shaped."""
    return code in _ALLOWED_CODES or _is_seller_specific_code(code)


# Anchor scan paths on the test file's location so they resolve correctly
# regardless of pytest's working directory (CI runs from the repo root;
# agents/IDEs may launch pytest from a subdir, which would make the relative
# paths silently match nothing).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = [
    _REPO_ROOT / "src/core/tools",
    _REPO_ROOT / "src/adapters",
]


from tests.unit._architecture_helpers import collect_error_aliases as _collect_error_aliases  # noqa: E402
from tests.unit._architecture_helpers import iter_call_expressions  # noqa: E402


def _collect_error_code_literals() -> list[tuple[str, int, str]]:
    """AST-scan for Error(code="...") and return (file, line, code) triples.

    Tracks `from ... import Error as <alias>` so call sites that use the
    aliased name (e.g. ``AdCPErrorDetail(code=...)``) are also validated.
    """
    violations: list[tuple[str, int, str]] = []

    for scan_dir in _SCAN_DIRS:
        if not scan_dir.exists():
            continue
        for py_file in sorted(scan_dir.rglob("*.py")):
            source = py_file.read_text()
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            error_aliases = _collect_error_aliases(tree)

            for node in iter_call_expressions(tree):  # Match calls to Error(...) / <alias>(...) / adcp.types.Error(...)
                func = node.func
                matched = False
                if isinstance(func, ast.Name) and func.id in error_aliases:
                    matched = True
                elif isinstance(func, ast.Attribute) and func.attr == "Error":
                    matched = True
                if not matched:
                    continue

                # Extract the code= keyword argument
                code_value = None
                for kw in node.keywords:
                    if kw.arg == "code":
                        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                            code_value = kw.value.value
                        else:
                            # Non-literal code — skip with warning
                            logger.warning(
                                "%s:%d: Error(code=<non-literal>) — cannot validate statically",
                                py_file,
                                node.lineno,
                            )
                        break

                if code_value is not None and not _code_is_compliant(code_value):
                    violations.append((str(py_file), node.lineno, code_value))

    return violations


class TestErrorCodeCompliance:
    """Every Error(code=...) literal must be in ``_ALLOWED_CODES``."""

    @pytest.mark.arch_guard
    def test_no_nonstandard_error_codes_in_tools_and_adapters(self):
        """AST-scan Error(code=...) sites for codes outside ``_ALLOWED_CODES``."""
        violations = _collect_error_code_literals()
        if violations:
            msg_lines = [f"  {f}:{line}: code={code!r}" for f, line, code in violations]
            raise AssertionError(
                f"{len(violations)} Error(code=...) sites use non-standard codes:\n"
                + "\n".join(msg_lines)
                + "\n\nEach code must be in _ALLOWED_CODES "
                "(WIRE_STANDARD_CODES ∪ INTERNAL_CODES ∪ _SPEC_CODES) or match the "
                "seller-specific shape X_{VENDOR}_{CODE} (transport-errors.mdx § "
                "Seller-Specific Error Codes, AdCP 3.1.1)."
            )

    @pytest.mark.arch_guard
    def test_adcp_error_subclass_codes_are_compliant(self):
        """Every AdCPError subclass _default_error_code must be standard or internal.

        Reads ``_default_error_code`` (the ClassVar slot per option-A refactor
        ). The public ``error_code`` is an instance attribute
        set in ``__init__`` and is not present on the class object.
        """
        from src.core.exceptions import AdCPError

        violations = []
        queue = [AdCPError]
        while queue:
            cls = queue.pop()
            for sub in cls.__subclasses__():
                code = sub._default_error_code
                if not _code_is_compliant(code):
                    violations.append(f"{sub.__name__}._default_error_code = {code!r}")
                queue.append(sub)

        assert not violations, "AdCPError subclasses with non-compliant codes:\n" + "\n".join(
            f"  {v}" for v in violations
        )


class TestSellerSpecificCodeShape:
    """The seller-specific predicate enforces a SHAPE, not a membership list.

    Proves the exact bug the prior ``_PLATFORM_SPECIFIC_CODES`` bare-set design
    could not catch: an arbitrary made-up string, or a value inside the
    standard ``CREATIVE_*`` namespace, must be rejected — only the pinned
    ``X_{VENDOR}_{CODE}`` shape passes.
    """

    def test_rejects_arbitrary_made_up_code(self):
        """A membership set would pass this if simply added to it; the shape predicate cannot."""
        assert not _is_seller_specific_code("TOTALLY_MADE_UP_CODE")

    def test_rejects_standard_namespace_collision(self):
        """The original bug: a platform code shaped like the standard CREATIVE_* vocabulary."""
        assert not _is_seller_specific_code("CREATIVE_GEMINI_KEY_MISSING")

    def test_rejects_missing_vendor_prefix(self):
        assert not _is_seller_specific_code("PREBID_CREATIVE_GEMINI_KEY_MISSING")

    def test_accepts_the_renamed_production_code(self):
        assert _is_seller_specific_code("X_PREBID_CREATIVE_GEMINI_KEY_MISSING")

    def test_rejects_lowercase(self):
        assert not _is_seller_specific_code("x_prebid_creative_gemini_key_missing")

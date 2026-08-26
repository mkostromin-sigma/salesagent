"""UC-021 Preview Creative — feature contract only (no harness binder).

``BR-UC-021-preview-creative.feature`` documents the AdCP error contract for
format misses (``REFERENCE_NOT_FOUND`` + generic message) for when a
``preview_creative`` tool / ``_detect_uc`` arm lands. Salesagent does not ship
that tool today (#1847 non-goal), and binding via ``scenarios()`` without a
harness produced hundreds of auto-xfails that read as coverage.

Do **not** re-add ``scenarios(...)`` here until ``_detect_uc`` gains a UC-021
arm and step modules can drive the preview path. Create-path format-miss
grading lives under UC-002 (``@T-UC-002-ext-h-format`` / H-03).
"""

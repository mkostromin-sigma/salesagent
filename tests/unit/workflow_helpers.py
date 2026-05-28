"""Shared helpers for CI workflow guard tests."""

from __future__ import annotations

from pathlib import Path

import yaml

CI_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def load_ci_workflow() -> dict:
    """Load and parse the canonical CI workflow file."""
    return yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))

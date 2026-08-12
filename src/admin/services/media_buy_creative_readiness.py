"""Admin facade for creative finalize-readiness (Flask flash).

Domain policy and the approve finalize orchestrator live in
``src.services.media_buy_creative_readiness``. This module adds only the
Flask-aware flash helper used by operations / workflows hold arms.
"""

from __future__ import annotations

from flask import flash

__all__ = ["flash_creative_finalize_hold"]


def flash_creative_finalize_hold(hold_message: str | None) -> None:
    """Flash the hold message after ``finalize_media_buy_approval`` returns held."""
    # Domain invariant: every not-ready result carries a non-empty hold_message.
    if not hold_message:
        raise ValueError("hold_message required when flashing creative finalize hold")
    flash(hold_message, "info")

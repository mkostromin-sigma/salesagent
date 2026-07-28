"""Admin facade for creative finalize-readiness (Flask flash / session apply).

Domain policy lives in ``src.services.media_buy_creative_readiness``. This module
re-exports domain symbols for any lingering admin imports and adds the
Flask-aware apply helper used by operations / workflows hold arms.
"""

from __future__ import annotations

from flask import flash

from src.core.database.models import MediaBuy
from src.services.media_buy_creative_readiness import (
    CreativeFinalizeReadiness,
    HoldReason,
    apply_creative_finalize_hold,
    compute_media_buy_status_from_flight_dates,
    evaluate_creative_finalize_readiness,
    evaluate_creative_finalize_readiness_for_session,
    log_creative_finalize_hold,
)

__all__ = [
    "CreativeFinalizeReadiness",
    "HoldReason",
    "apply_creative_finalize_hold",
    "apply_creative_finalize_hold_for_admin",
    "compute_media_buy_status_from_flight_dates",
    "evaluate_creative_finalize_readiness",
    "evaluate_creative_finalize_readiness_for_session",
    "log_creative_finalize_hold",
]


def apply_creative_finalize_hold_for_admin(
    media_buy: MediaBuy,
    readiness: CreativeFinalizeReadiness,
    *,
    approved_by: str,
    db_session,
) -> None:
    """Apply domain hold, flash the hold message, and commit the admin session."""
    apply_creative_finalize_hold(media_buy, readiness, approved_by=approved_by)
    # Domain invariant: every not-ready result carries a non-empty hold_message.
    if not readiness.hold_message:
        raise ValueError("hold_message required when applying creative finalize hold")
    flash(readiness.hold_message, "info")
    db_session.commit()

"""Admin facade for creative finalize-readiness (Flask flash / session apply).

Domain policy lives in ``src.services.media_buy_creative_readiness``. This module
adds only the Flask-aware apply helper used by operations / workflows hold arms.
"""

from __future__ import annotations

from flask import flash
from sqlalchemy.orm import Session

from src.core.database.models import MediaBuy
from src.services.media_buy_creative_readiness import (
    CreativeFinalizeReadiness,
    apply_creative_finalize_hold,
)

__all__ = ["apply_creative_finalize_hold_for_admin"]


def apply_creative_finalize_hold_for_admin(
    media_buy: MediaBuy,
    readiness: CreativeFinalizeReadiness,
    *,
    approved_by: str,
    db_session: Session,
) -> None:
    """Apply domain hold, flash the hold message, and commit the admin session."""
    apply_creative_finalize_hold(media_buy, readiness, approved_by=approved_by)
    # Domain invariant: every not-ready result carries a non-empty hold_message.
    if not readiness.hold_message:
        raise ValueError("hold_message required when applying creative finalize hold")
    flash(readiness.hold_message, "info")
    db_session.commit()

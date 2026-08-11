"""Domain creative finalize-readiness predicate for media-buy approve paths.

Shared by admin workflows / operations / creatives so zero-assignment and
unapproved-creative hold decisions share one policy (issue #1696). Neutral
module (not Flask-aware) — admin flash/commit lives in the admin facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, cast

from sqlalchemy.orm import Session

from src.core.database.database_session import get_db_session
from src.core.database.models import MediaBuy
from src.core.database.repositories.creative import (
    CreativeAssignmentRepository,
    CreativeRepository,
)
from src.core.database.repositories.media_buy import MediaBuyRepository
from src.core.schemas.creative import FINALIZE_READY_CREATIVE_STATUSES
from src.core.utils import utc_flight_end, utc_flight_start

logger = logging.getLogger(__name__)

HoldReason = Literal["no_assignments", "unapproved_creatives"]

_HOLD_MSG_NO_ASSIGNMENTS = (
    "Media buy approved! Waiting for creatives to be assigned and approved before creating in GAM."
)


@dataclass(frozen=True)
class CreativeFinalizeReadiness:
    """Result of evaluating whether a media buy may proceed to adapter finalize."""

    ready: bool
    """True iff ≥1 assignment AND every linked creative is in the allowlist."""

    unapproved_creative_ids: list[str]
    hold_reason: HoldReason | None
    hold_message: str | None = None


def _hold_message_for(reason: HoldReason, unapproved_count: int) -> str:
    if reason == "no_assignments":
        return _HOLD_MSG_NO_ASSIGNMENTS
    return f"Media buy approved! Waiting for {unapproved_count} creative(s) to be approved before creating in GAM."


def evaluate_creative_finalize_readiness(
    assignments_repo: CreativeAssignmentRepository,
    creatives_repo: CreativeRepository,
    *,
    media_buy_id: str,
) -> CreativeFinalizeReadiness:
    """Evaluate whether creatives are ready for media-buy finalize / adapter create.

    Locked Hold semantics (#1696): zero CreativeAssignment rows ⇒ not ready
    (``hold_reason="no_assignments"``). Repositories are tenant-scoped; creative
    loads use the composite key via ``get_by_ids(..., principal_id)``.
    """
    assignments = assignments_repo.get_by_media_buy(media_buy_id)

    if not assignments:
        return CreativeFinalizeReadiness(
            ready=False,
            unapproved_creative_ids=[],
            hold_reason="no_assignments",
            hold_message=_hold_message_for("no_assignments", 0),
        )

    # Group by principal so each get_by_ids call matches the composite PK.
    by_principal: dict[str, list[str]] = {}
    for assignment in assignments:
        by_principal.setdefault(assignment.principal_id, []).append(assignment.creative_id)

    creatives = []
    for principal_id, creative_ids in by_principal.items():
        creatives.extend(creatives_repo.get_by_ids(creative_ids, principal_id))

    # dict preserves first-seen order; membership is O(1) (list `in` was O(n)).
    unapproved_ids: dict[str, None] = {
        c.creative_id: None for c in creatives if c.status not in FINALIZE_READY_CREATIVE_STATUSES
    }
    # Missing creative rows (assignment points at deleted/missing) count as not ready.
    found_ids = {c.creative_id for c in creatives}
    for cid in (a.creative_id for a in assignments):
        if cid not in found_ids:
            unapproved_ids[cid] = None
    unapproved_creative_ids = list(unapproved_ids)

    if unapproved_creative_ids:
        return CreativeFinalizeReadiness(
            ready=False,
            unapproved_creative_ids=unapproved_creative_ids,
            hold_reason="unapproved_creatives",
            hold_message=_hold_message_for("unapproved_creatives", len(unapproved_creative_ids)),
        )

    return CreativeFinalizeReadiness(
        ready=True,
        unapproved_creative_ids=[],
        hold_reason=None,
        hold_message=None,
    )


def evaluate_creative_finalize_readiness_for_session(
    session: Session,
    tenant_id: str,
    *,
    media_buy_id: str,
) -> CreativeFinalizeReadiness:
    """Session-level entry: construct tenant-scoped repos and evaluate readiness."""
    return evaluate_creative_finalize_readiness(
        CreativeAssignmentRepository(session, tenant_id),
        CreativeRepository(session, tenant_id),
        media_buy_id=media_buy_id,
    )


def log_creative_finalize_hold(
    media_buy_id: str,
    readiness: CreativeFinalizeReadiness,
    *,
    context_tag: str = "[APPROVAL]",
) -> None:
    """Log a finalize hold with an approval-trail tag and stable event key."""
    logger.info(
        "%s Creative finalize hold for media buy %s hold_reason=%s unapproved=%s event=creative_finalize_hold",
        context_tag,
        media_buy_id,
        readiness.hold_reason,
        readiness.unapproved_creative_ids,
    )


def stamp_media_buy_approval(media_buy: MediaBuy, *, approved_by: str) -> None:
    """Stamp approval provenance once — shared by hold and ready arms."""
    media_buy.approved_at = datetime.now(UTC)
    media_buy.approved_by = approved_by


def apply_creative_finalize_hold(
    media_buy: MediaBuy,
    readiness: CreativeFinalizeReadiness,
    *,
    approved_by: str,
) -> None:
    """Apply hold outcome: provenance + pending_creatives + single info log."""
    stamp_media_buy_approval(media_buy, approved_by=approved_by)
    media_buy.status = "pending_creatives"
    log_creative_finalize_hold(media_buy.media_buy_id, readiness)


def apply_creative_finalize_ready(media_buy: MediaBuy, *, approved_by: str) -> None:
    """Apply ready outcome: provenance + flight-window status (mirror of hold)."""
    stamp_media_buy_approval(media_buy, approved_by=approved_by)
    media_buy.status = compute_media_buy_status_from_flight_dates(media_buy)


def mark_media_buy_adapter_failed(
    media_buy_id: str,
    tenant_id: str,
    *,
    error_msg: str | None = None,
) -> None:
    """Roll back to ``failed`` and log after adapter execute fails on the ready arm.

    ``apply_creative_finalize_ready`` commits the optimistic flight-window
    status before adapter execute runs (so a failed adapter still records who
    approved the buy). This undoes that status and emits one ``[APPROVAL]``
    ERROR line — shared by every ready-arm caller so an adapter failure leaves
    the same persisted status and the same operator trail regardless of which
    admin surface approved the buy.
    """
    logger.error("[APPROVAL] Adapter creation failed for %s: %s", media_buy_id, error_msg)
    with get_db_session() as session:
        repo = MediaBuyRepository(session, tenant_id)
        if repo.update_status(media_buy_id, "failed"):
            session.commit()


def _coerce_flight_boundary(
    dt: datetime | None,
    date_value: date | None,
    *,
    end_of_day: bool,
) -> datetime | None:
    """Normalize a start/end boundary from aware/naive datetime or date column."""
    if dt:
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    if date_value:
        return utc_flight_end(date_value) if end_of_day else utc_flight_start(date_value)
    return None


def compute_media_buy_status_from_flight_dates(media_buy: MediaBuy) -> str:
    """Compute post-approve status from flight window: active / scheduled / completed."""
    now = datetime.now(UTC)

    # MediaBuy annotates start_date/end_date as Mapped[Date] (SQLAlchemy type), not
    # Mapped[date]; runtime values are datetime.date. Cast bridges the model typo.
    start_time = _coerce_flight_boundary(
        media_buy.start_time,
        cast(date | None, media_buy.start_date),
        end_of_day=False,
    )
    end_time = _coerce_flight_boundary(
        media_buy.end_time,
        cast(date | None, media_buy.end_date),
        end_of_day=True,
    )

    if start_time and end_time:
        if now > end_time:
            return "completed"
        if now >= start_time:
            return "active"
        return "scheduled"

    if start_time and now < start_time:
        return "scheduled"
    if end_time and now > end_time:
        return "completed"
    return "active"

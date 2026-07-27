"""Shared creative finalize-readiness predicate for admin media-buy approve paths.

Used by workflows / operations / creatives blueprints so zero-assignment and
unapproved-creative hold decisions share one policy (issue #1696).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from src.core.database.repositories.creative import (
    CreativeAssignmentRepository,
    CreativeRepository,
)
from src.core.schemas.creative import FINALIZE_READY_CREATIVE_STATUSES

HoldReason = Literal["no_assignments", "unapproved_creatives"]

_HOLD_MSG_NO_ASSIGNMENTS = (
    "Media buy approved! Waiting for creatives to be assigned and approved before creating in GAM."
)


@dataclass(frozen=True)
class CreativeFinalizeReadiness:
    """Result of evaluating whether a media buy may proceed to adapter finalize."""

    ready: bool
    """True iff ≥1 assignment AND every linked creative is in the allowlist."""

    assignment_count: int
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
    assignment_count = len(assignments)

    if assignment_count == 0:
        return CreativeFinalizeReadiness(
            ready=False,
            assignment_count=0,
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

    unapproved_creative_ids = [c.creative_id for c in creatives if c.status not in FINALIZE_READY_CREATIVE_STATUSES]
    # Missing creative rows (assignment points at deleted/missing) count as not ready.
    found_ids = {c.creative_id for c in creatives}
    for cid in (a.creative_id for a in assignments):
        if cid not in found_ids and cid not in unapproved_creative_ids:
            unapproved_creative_ids.append(cid)

    if unapproved_creative_ids:
        return CreativeFinalizeReadiness(
            ready=False,
            assignment_count=assignment_count,
            unapproved_creative_ids=unapproved_creative_ids,
            hold_reason="unapproved_creatives",
            hold_message=_hold_message_for("unapproved_creatives", len(unapproved_creative_ids)),
        )

    return CreativeFinalizeReadiness(
        ready=True,
        assignment_count=assignment_count,
        unapproved_creative_ids=[],
        hold_reason=None,
        hold_message=None,
    )


def should_hold_media_buy_for_creatives(
    assignments_repo: CreativeAssignmentRepository,
    creatives_repo: CreativeRepository,
    *,
    media_buy_id: str,
) -> bool:
    """True when approve must park the buy (not call execute_approved_media_buy)."""
    return not evaluate_creative_finalize_readiness(assignments_repo, creatives_repo, media_buy_id=media_buy_id).ready


def compute_media_buy_status_from_flight_dates(media_buy) -> str:
    """Compute post-approve status from flight window: active / scheduled / completed."""
    now = datetime.now(UTC)

    start_time = None
    if media_buy.start_time:
        raw_start = media_buy.start_time
        start_time = raw_start.replace(tzinfo=UTC) if raw_start.tzinfo is None else raw_start.astimezone(UTC)
    elif getattr(media_buy, "start_date", None):
        start_time = datetime.combine(media_buy.start_date, datetime.min.time()).replace(tzinfo=UTC)

    end_time = None
    if media_buy.end_time:
        raw_end = media_buy.end_time
        end_time = raw_end.replace(tzinfo=UTC) if raw_end.tzinfo is None else raw_end.astimezone(UTC)
    elif getattr(media_buy, "end_date", None):
        end_time = datetime.combine(media_buy.end_date, datetime.max.time()).replace(tzinfo=UTC)

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

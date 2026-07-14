"""
Delivery Webhook Scheduler

Sends daily delivery reports via webhooks for media buys that have configured reporting_webhook.
This runs as a background task and sends reports when GAM data is fresh (after 4 AM PT daily).
"""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from adcp import create_mcp_webhook_payload
from adcp.types import GeneratedTaskStatus as AdcpTaskStatus
from adcp.types import MediaBuyStatus
from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (
    NotificationType,
)  # TODO: no stable alias — response-level NotificationType differs from top-level
from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.database.models import PushNotificationConfig as DBPushNotificationConfig
from src.core.database.repositories import MediaBuyRepository
from src.core.database.repositories.delivery import DeliveryRepository
from src.core.helpers import enum_value
from src.core.schemas import GetMediaBuyDeliveryRequest, GetMediaBuyDeliveryResponse
from src.core.tools._media_buy_status import (
    CANONICAL_COMPLETED,
    CANONICAL_SERVING,
    SERVING_PERSISTED_STATUSES,
    derive_notification_type,
    resolve_canonical_status,
)
from src.core.tools.media_buy_delivery import _get_media_buy_delivery_impl
from src.core.utils import utc_flight_start
from src.services.protocol_webhook_service import get_protocol_webhook_service

logger = logging.getLogger(__name__)

# 1 hour because AdCP protocol has frequency options hourly, daily and monthly
# Configurable via env var for testing
SLEEP_INTERVAL_SECONDS = int(os.getenv("DELIVERY_WEBHOOK_INTERVAL") or "3600")

# The canonical statuses the delivery impl reports on — used both as the
# impl request's status_filter and as the scheduler's pre-send skip: a
# selected buy resolving outside this set (pre-flight pending_start, paused)
# has no delivery data, and asking the impl for it produces a
# MEDIA_BUY_NOT_FOUND advisory for a buy that exists.
REPORTABLE_CANONICAL_STATUSES: frozenset[str] = frozenset({CANONICAL_SERVING, CANONICAL_COMPLETED})


class DeliveryWebhookScheduler:
    """Scheduler for sending delivery reports via webhooks."""

    def __init__(self) -> None:
        self.webhook_service = get_protocol_webhook_service()
        self.is_running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the scheduler background task."""
        async with self._lock:
            if self.is_running:
                logger.warning("Delivery webhook scheduler is already running")
                return

            self.is_running = True
            self._task = asyncio.create_task(self._run_scheduler())
            logger.info("Delivery webhook scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler background task."""
        async with self._lock:
            if not self.is_running:
                return

            self.is_running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            logger.info("Delivery webhook scheduler stopped")

    async def _run_scheduler(self) -> None:
        """Main scheduler loop - runs on a fixed hourly cadence.

        Sends immediately on startup (duplicate check prevents re-sending if
        already sent in last 24 hours), then continues on hourly cadence.
        """
        while self.is_running:
            try:
                await self._send_reports()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in delivery webhook scheduler: {e}", exc_info=True)
            finally:
                # Wait before next batch
                await asyncio.sleep(SLEEP_INTERVAL_SECONDS)

    async def _send_reports(self) -> tuple[int, int]:
        """Send reports for all active media buys with configured webhooks.

        Returns:
            ``(reports_sent, errors)`` tally for this batch run — callers
            (tests, callers wanting the outcome instead of just the log line)
            read this instead of re-deriving it from log output.
        """
        logger.info("Starting scheduled delivery report webhook batch")

        reports_sent = 0
        errors = 0

        try:
            with get_db_session() as session:
                # Find all serving media buys (cross-tenant scheduler query).
                # Uses the derived serving set so legacy aliases ("ready" /
                # "scheduled") are included — a hardcoded partial list stranded
                # them without webhooks (#1556).
                media_buys = MediaBuyRepository.get_all_by_statuses(session, sorted(SERVING_PERSISTED_STATUSES))

                for media_buy in media_buys:
                    try:
                        # Check if this media buy has a reporting webhook configured
                        raw_request = media_buy.raw_request or {}
                        reporting_webhook = raw_request.get("reporting_webhook")

                        if not reporting_webhook:
                            continue

                        # The status-only selection also matches pre-flight and
                        # paused rows the impl cannot report on. Resolve the
                        # same canonical status the impl would and skip them
                        # here, instead of invoking the full delivery impl
                        # every hour only to misread its MEDIA_BUY_NOT_FOUND
                        # advisory as a warning-worthy failure.
                        canonical = resolve_canonical_status(media_buy, datetime.now(UTC).date())
                        if canonical not in REPORTABLE_CANONICAL_STATUSES:
                            continue

                        # Send delivery report; only count it when a webhook
                        # actually went out (dedup/frequency skips return False).
                        if await self._send_report_for_media_buy(media_buy, reporting_webhook, session):
                            reports_sent += 1

                    except Exception as e:
                        logger.error(f"Error sending report for media buy {media_buy.media_buy_id}: {e}", exc_info=True)
                        errors += 1

                logger.info(f"Daily delivery report batch complete: {reports_sent} sent, {errors} errors")

        except Exception as e:
            logger.error(f"Error in daily delivery report batch: {e}", exc_info=True)

        return reports_sent, errors

    async def trigger_report_for_media_buy_by_id(self, media_buy_id: str, tenant_id: str) -> bool:
        """Manually trigger a delivery report for a single media buy by ID.

        This method manages its own database session to avoid detached instance errors.

        Args:
            media_buy_id: The media buy ID
            tenant_id: The tenant ID

        Returns:
            bool: True if report was triggered successfully, False otherwise
        """
        try:
            with get_db_session() as session:
                repo = MediaBuyRepository(session, tenant_id)
                media_buy = repo.get_by_id(media_buy_id)

                if not media_buy:
                    logger.warning(f"Cannot trigger report: Media buy {media_buy_id} not found")
                    return False

                raw_request = media_buy.raw_request or {}
                reporting_webhook = raw_request.get("reporting_webhook")

                if not reporting_webhook:
                    logger.warning(f"Cannot trigger report: No reporting_webhook configured for {media_buy_id}")
                    return False

                # Force sending even if already sent today (for testing)
                return await self._send_report_for_media_buy(media_buy, reporting_webhook, session, force=True)
        except Exception as e:
            logger.error(f"Error manually triggering report for {media_buy_id}: {e}", exc_info=True)
            return False

    async def _send_report_for_media_buy(
        self,
        media_buy: Any,
        reporting_webhook: dict,
        session: Any,
        force: bool = False,
        notification_type_override: str | None = None,
    ) -> bool:
        """Send a delivery report for a single media buy.

        Args:
            media_buy: MediaBuy database model
            reporting_webhook: Webhook configuration dict
            session: Database session
            force: If True, bypass frequency checks and duplicate checks
            notification_type_override: When ``"final"``, forces the outgoing
                notification_type to "final" regardless of what the delivery
                impl's reported statuses would derive, and widens the delivery
                request's status filter so a buy already transitioned to
                completed/rejected/canceled/failed is still found (#1606
                point 9). Used by ``enqueue_final_if_configured`` — the
                proactive final send fired at the moment a media buy
                transitions into a NO_MORE_DATA status, instead of relying on
                the hourly batch to catch the (often momentary) window where
                the persisted status still qualifies for the SERVING query.

        Returns:
            True when a webhook was actually delivered; False when the buy was
            legitimately skipped (unsupported frequency, dedup, one-shot final
            already sent/in-flight, no data, no URL). A failed delivery RAISES
            so the caller counts it as an error instead of a send.
        """
        is_final_send = notification_type_override == "final"
        try:
            repo = DeliveryRepository(session, media_buy.tenant_id)

            # One-shot final guard (#1606 points 8/12): a final already
            # delivered ("success") or in-flight ("reserved"/"retrying")
            # permanently blocks ANY further send for this stream — a stray
            # "scheduled" attempt after the terminal notification went out,
            # a repeat final from a second transition-enqueue call, AND a
            # force=True manual trigger. Checked before frequency/dedup so a
            # forced call can never race past it.
            if repo.has_final_notification(media_buy.media_buy_id, task_type="media_buy_delivery"):
                logger.info(
                    "Skipping delivery webhook for media buy %s – a final notification was already "
                    "delivered or is in flight",
                    media_buy.media_buy_id,
                )
                return False

            # Determine reporting frequency from AdCP config. The spec field
            # is "reporting_frequency" (ReportingWebhook.reporting_frequency);
            # "frequency" is kept as a fallback for rows persisted before this
            # fix landed (#1606 point 11).
            raw_freq = str(
                reporting_webhook.get("reporting_frequency") or reporting_webhook.get("frequency") or "daily"
            ).lower()

            # A final notification is a one-time terminal event, not a
            # recurring cadence — it is not subject to the frequency gate.
            if not force and not is_final_send and raw_freq != "daily":
                logger.warning(
                    "Skipping reporting webhook with frequency '%s' for media buy %s – "
                    "only 'daily' frequency is supported for delivery webhooks at this time",
                    raw_freq,
                    media_buy.media_buy_id,
                )
                return False

            # Calculate reporting period for daily frequency: yesterday (full day)
            start_date_obj = datetime.now(UTC).date() - timedelta(days=1)
            end_date_obj = datetime.now(UTC)

            # 24h rolling-window dedup applies to "scheduled" sends only
            # (#1606 point 8) — "final" is governed exclusively by the
            # one-shot has_final_notification guard above, not a time window.
            if not force and not is_final_send:
                one_day_ago = datetime.now(UTC) - timedelta(hours=24)
                existing_log = repo.get_recent_successful_log(
                    media_buy.media_buy_id,
                    task_type="media_buy_delivery",
                    since=one_day_ago,
                    notification_type="scheduled",
                )
                if existing_log:
                    logger.info(
                        "Skipping daily delivery webhook for media buy %s and date %s – already sent (log id %s)",
                        media_buy.media_buy_id,
                        end_date_obj,
                        existing_log.id,
                    )
                    return False

            # Fetch delivery metrics
            # Create a ResolvedIdentity for the delivery call
            from src.core.resolved_identity import ResolvedIdentity

            identity = ResolvedIdentity(
                principal_id=media_buy.principal_id,
                tenant_id=media_buy.tenant_id,
                tenant={"tenant_id": media_buy.tenant_id},
                protocol="rest",
            )

            # The impl reports on exactly REPORTABLE_CANONICAL_STATUSES: the
            # scheduler already filters by persisted DB status
            # (SERVING_PERSISTED_STATUSES) at query time and skips buys that
            # resolve outside the reportable set, so ended campaigns (dynamic
            # status=completed) are included rather than filtered out and
            # reported as "not found" errors.
            #
            # A final send instead passes status_filter=None: with explicit
            # media_buy_ids and no status_filter, the impl uses fetch-by-ID
            # semantics (returns the buy regardless of status), which is
            # required here because the buy may already be persisted as
            # rejected/canceled/failed — none of which are valid
            # MediaBuyStatus enum members for a status_filter (only
            # completed is, of the terminal statuses).
            req = GetMediaBuyDeliveryRequest(
                media_buy_ids=[media_buy.media_buy_id],
                status_filter=None if is_final_send else [MediaBuyStatus(s) for s in REPORTABLE_CANONICAL_STATUSES],
                start_date=start_date_obj.strftime("%Y-%m-%d"),
                end_date=end_date_obj.strftime("%Y-%m-%d"),
                context=None,
            )

            delivery_response = _get_media_buy_delivery_impl(req, identity)

            if not isinstance(delivery_response, GetMediaBuyDeliveryResponse):
                logger.warning(
                    f"`Couldn't get media_delivery` for {media_buy.media_buy_id}. Result is {delivery_response.model_dump()}"
                )
                return False

            if delivery_response.errors is not None:
                logger.warning(
                    f"`Couldn't get media_delivery` for {media_buy.media_buy_id}. We have recieved error in the result. Result is {delivery_response.model_dump()}"
                )
                return False

            # Extract webhook URL and authentication
            webhook_url = reporting_webhook.get("url")
            if not webhook_url:
                logger.warning(f"No webhook URL configured for media buy {media_buy.media_buy_id}")
                return False

            # Set webhook-specific metadata directly on the response model (#1570).
            # These fields are webhook-only ("only present in webhook deliveries" —
            # get-media-buy-delivery-response.json @ v3.1-04f59d2d5), so the polling
            # impl never sets them; this webhook path is the single place they are
            # attached to the wire.
            #
            # notification_type: an explicit override (the transition-triggered
            # final send) wins outright; otherwise derived from the reported
            # statuses — "final" when every buy will never produce more data
            # ("one final notification when the campaign completes",
            # optimization-reporting.mdx §Publisher Commitment), "scheduled"
            # otherwise.
            derived = notification_type_override or derive_notification_type(
                enum_value(d.status) for d in delivery_response.media_buy_deliveries or []
            )
            delivery_response.notification_type = NotificationType(derived) if derived else None

            # next_expected_at: only present when notification_type is not "final"
            # (spec, same schema — a non-nullable date-time, so a final webhook
            # must OMIT the field; leaving it None lets the response's
            # exclude-None serialization drop it from the wire). Daily
            # frequency -> start of next day (UTC).
            if derived == "final":
                delivery_response.next_expected_at = None
            elif derived == "scheduled":
                next_day = datetime.now(UTC).date() + timedelta(days=1)
                delivery_response.next_expected_at = utc_flight_start(next_day)
            # derived is None (zero deliveries) -> leave next_expected_at unset;
            # notification_type is None too, so the pair stays consistent.

            # Durably reserve the sequence number for this outbox stream
            # BEFORE the HTTP call (#1606 point 3/6): allocated under an
            # advisory transaction lock as MAX(sequence over ALL statuses) + 1
            # — a "reserved" or "retrying" row already holds a number even
            # without a successful delivery — and committed immediately so the
            # reservation is durable and the lock releases promptly. Once
            # reserved, the number is embedded on the payload and never
            # re-derived, even if the HTTP send later succeeds.
            log_id, sequence_number = self._reserve_sequence(
                tenant_id=media_buy.tenant_id,
                media_buy_id=media_buy.media_buy_id,
                principal_id=media_buy.principal_id,
                webhook_url=webhook_url,
                notification_type=derived,
            )
            delivery_response.sequence_number = sequence_number
            delivery_response.partial_data = False  # TODO: Check for reporting_delayed status
            delivery_response.unavailable_count = 0  # TODO: Count reporting_delayed/failed deliveries

            # Try to find existing push notification config or create a temporary one
            auth_config = reporting_webhook.get("authentication", {})
            auth_type = None
            auth_token = None

            if auth_config:
                schemes = auth_config.get("schemes", [])
                auth_type = schemes[0] if schemes else None
                auth_token = auth_config.get("credentials")

            # Query for existing push notification config for this media buy
            config_stmt = select(DBPushNotificationConfig).where(
                DBPushNotificationConfig.principal_id == media_buy.principal_id,
                DBPushNotificationConfig.tenant_id == media_buy.tenant_id,
                DBPushNotificationConfig.url == webhook_url,
                DBPushNotificationConfig.is_active,
            )
            push_notification_config = session.scalars(config_stmt).first()

            # Extract webhook config data before session closes
            if push_notification_config:
                # Detach from session and extract data
                session.expunge(push_notification_config)
            else:
                # Create a detached temporary config (not attached to session)
                push_notification_config = DBPushNotificationConfig(
                    id=f"temp_{media_buy.media_buy_id}",
                    tenant_id=media_buy.tenant_id,
                    principal_id=media_buy.principal_id,
                    url=webhook_url,
                    authentication_type=auth_type,
                    authentication_token=auth_token,
                    is_active=True,
                )

            # Wire vs internal task_type distinction:
            # - metadata["task_type"] = "media_buy_delivery" -- internal logging/dedup label
            #   used by protocol_webhook_service guards and WebhookDeliveryLog queries.
            # - SDK task_type = "update_media_buy" -- AdCP spec TaskType enum value
            #   for the wire payload (delivery reports are status updates on media buys).
            # These are intentionally different: the internal label predates the SDK enum
            # and is used for DB filtering, while the wire value must be spec-compliant.
            # Renaming the metadata key is not safe without migrating DB records and
            # updating all 6 protocol_webhook_service guard checks.
            metadata = {
                "task_type": "media_buy_delivery",
                "tenant_id": media_buy.tenant_id,
                "principal_id": media_buy.principal_id,
                "media_buy_id": media_buy.media_buy_id,
                # Pre-reserved outbox row id (#1606): protocol_webhook_service
                # UPDATEs this row via mark_delivery_result instead of
                # creating a fresh one, so the reserved sequence number is
                # never re-derived after the HTTP call.
                "log_id": log_id,
            }

            # SDK 5.7: returns McpWebhookPayload directly; 3rd arg is task_type.
            # Delivery reports are status updates on existing media buys,
            # so we use update_media_buy as the canonical task type.
            media_buy_delivery_payload = create_mcp_webhook_payload(
                task_id=media_buy.media_buy_id,
                task_type="update_media_buy",
                result=delivery_response,
                status=AdcpTaskStatus.completed,
            )

            # Send webhook notification OUTSIDE the session context
            # This ensures the session is closed before async webhook call
            delivered = await self.webhook_service.send_notification(
                push_notification_config=push_notification_config, payload=media_buy_delivery_payload, metadata=metadata
            )

            if not delivered:
                # send_notification returns False (never raises) on permanent
                # 4xx / exhausted retries and has already written the failed
                # WebhookDeliveryLog row. Raise so the batch counts an error
                # instead of logging "Sent" for a webhook the buyer never got.
                raise RuntimeError(
                    f"Delivery report webhook send failed for media buy {media_buy.media_buy_id} "
                    "(see webhook service logs for the HTTP failure detail)"
                )

            logger.info(f"Sent delivery report webhook for media buy {media_buy.media_buy_id}")
            return True

        except Exception as e:
            # Re-raise for the caller (batch loop / manual trigger) to own the
            # single ERROR line. Log at DEBUG here to avoid a duplicate full
            # traceback on the common send_notification -> False path.
            logger.debug(f"Error sending delivery report for media buy {media_buy.media_buy_id}: {e}", exc_info=True)
            raise

    def _reserve_sequence(
        self,
        *,
        tenant_id: str,
        media_buy_id: str,
        principal_id: str,
        webhook_url: str,
        notification_type: str | None,
    ) -> tuple[str, int]:
        """Reserve (or reclaim) the outbox sequence for a delivery-webhook stream.

        Uses its own short-lived session distinct from the caller's
        long-lived batch/status-transition session: ``reserve_next_sequence``
        takes a Postgres advisory transaction lock that must be released by
        an immediate commit here, independent of whatever the caller's outer
        session is doing. Reclaims an existing reserved/retrying row for the
        same notification_type instead of minting a new sequence.
        """
        log_id = str(uuid4())
        with get_db_session() as reserve_session:
            log_entry = DeliveryRepository(reserve_session, tenant_id).reserve_next_sequence(
                media_buy_id=media_buy_id,
                task_type="media_buy_delivery",
                log_id=log_id,
                principal_id=principal_id,
                webhook_url=webhook_url,
                notification_type=notification_type,
            )
            sequence_number = log_entry.sequence_number
            log_id = log_entry.id
            reserve_session.commit()
        return log_id, sequence_number

    async def enqueue_final_if_configured(self, media_buy: Any, session: Any) -> bool:
        """Send exactly one final delivery webhook for a media buy that just
        transitioned into a NO_MORE_DATA status (#1606 point 9).

        The hourly batch alone is unreliable for this: once the persisted
        status flips to completed/canceled/rejected/failed it drops out of
        ``SERVING_PERSISTED_STATUSES`` and is never selected again, so unless
        the batch happens to run in the brief window between the flight
        ending and the status scheduler persisting the flip, the buyer's
        required final notification is silently never sent. Call this
        directly from every site that persists such a transition
        (``media_buy_status_scheduler``, admin approve/reject/adapter-failure
        paths) right after the status commit.

        Idempotent and safe to call from multiple transition sites or
        multiple times: ``_send_report_for_media_buy``'s one-shot
        ``has_final_notification`` guard blocks a second final outright.
        Best-effort — a failure here must not unwind the status transition
        that already committed, so exceptions are caught and logged, not
        propagated.

        Returns:
            True if a final webhook was sent by this call; False if none was
            configured, one already existed, or the send failed/erred.
        """
        raw_request = media_buy.raw_request or {}
        reporting_webhook = raw_request.get("reporting_webhook")
        if not reporting_webhook:
            return False
        try:
            return await self._send_report_for_media_buy(
                media_buy, reporting_webhook, session, force=True, notification_type_override="final"
            )
        except Exception:
            logger.error(
                "Failed to enqueue final delivery webhook for media buy %s", media_buy.media_buy_id, exc_info=True
            )
            return False


# Global scheduler instance
_scheduler: DeliveryWebhookScheduler | None = None


def get_delivery_webhook_scheduler() -> DeliveryWebhookScheduler:
    """Get or create global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = DeliveryWebhookScheduler()
    return _scheduler


async def start_delivery_webhook_scheduler():
    """Start the delivery webhook scheduler (called at application startup)."""
    scheduler = get_delivery_webhook_scheduler()
    await scheduler.start()


async def stop_delivery_webhook_scheduler():
    """Stop the delivery webhook scheduler (called at application shutdown)."""
    scheduler = get_delivery_webhook_scheduler()
    await scheduler.stop()

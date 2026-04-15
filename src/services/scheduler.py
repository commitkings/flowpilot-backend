"""
Scheduled-run dispatcher.

Runs as a background asyncio task started during app lifespan.
Every 60 seconds it polls for ScheduledRunModel rows whose
next_run_at is in the past and is_active=True, then fires them
by creating a new AgentRunModel and kicking off the orchestrator.

Requires the `croniter` package:
    pip install croniter

If croniter is not installed, next_run_at is not recomputed after
each fire and scheduling silently stops.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None


def _compute_next(cron_expr: str, after: Optional[datetime] = None) -> Optional[datetime]:
    try:
        from croniter import croniter
        base = after or datetime.now(timezone.utc)
        return croniter(cron_expr, base).get_next(datetime).replace(tzinfo=timezone.utc)
    except ImportError:
        logger.warning("[Scheduler] croniter not installed — cannot compute next run time")
        return None
    except Exception as exc:
        logger.warning(f"[Scheduler] Invalid cron expression '{cron_expr}': {exc}")
        return None


async def _fire_run(scheduled_id: uuid.UUID, business_id: uuid.UUID, objective: str) -> None:
    """Create and kick off a new AgentRunModel for the scheduled run."""
    try:
        from src.infrastructure.database.connection import get_session_factory
        from src.infrastructure.database.flowpilot_models import (
            AgentRunModel,
            BusinessMemberModel,
            BusinessModel,
        )
        from src.config.settings import Settings
        from src.agents.orchestrator import RunOrchestrator
        from src.agents.event_publisher import EventPublisher

        async with get_session_factory()() as session:
            # Look up the business to get merchant_id
            biz_result = await session.execute(
                select(BusinessModel).where(BusinessModel.id == business_id)
            )
            business = biz_result.scalars().first()
            if not business:
                logger.warning(
                    f"[Scheduler] Business {business_id} not found for scheduled run {scheduled_id}"
                )
                return

            # Find the business owner to use as created_by (required, NOT NULL)
            owner_result = await session.execute(
                select(BusinessMemberModel).where(
                    BusinessMemberModel.business_id == business_id,
                    BusinessMemberModel.role == "owner",
                    BusinessMemberModel.is_active.is_(True),
                ).limit(1)
            )
            owner_membership = owner_result.scalars().first()
            if not owner_membership:
                logger.warning(
                    f"[Scheduler] No active owner found for business {business_id}; "
                    f"skipping scheduled run {scheduled_id}"
                )
                return

            merchant_id = (
                getattr(business, "merchant_id", None)
                or getattr(Settings, "INTERSWITCH_MERCHANT_ID", None)
                or ""
            )

            run = AgentRunModel(
                business_id=business_id,
                created_by=owner_membership.user_id,
                objective=objective,
                merchant_id=merchant_id,
                status="pending",
                risk_tolerance=0.35,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)

            run_id = str(run.id)
            logger.info(
                f"[Scheduler] Fired scheduled run {scheduled_id} → new run {run_id}"
            )

            # Build initial agent state (matches the shape used in runs.py)
            state = {
                "run_id": run_id,
                "business_id": str(business_id),
                "objective": objective,
                "constraints": None,
                "date_from": None,
                "date_to": None,
                "risk_tolerance": 0.35,
                "budget_cap": None,
                "merchant_id": merchant_id,
                "plan_steps": [],
                "transactions": [],
                "reconciled_ledger": {},
                "unresolved_references": [],
                "resolved_references": [],
                "scored_candidates": [],
                "forecast": None,
                "candidate_lookup_results": [],
                "candidate_execution_results": [],
                "batch_details": None,
                "approved_candidate_ids": [],
                "rejected_candidate_ids": [],
                "audit_report": None,
                "current_step": "created",
                "error": None,
                "audit_entries": [],
                "reasoning_log": [],
            }

            # Run orchestrator within the same session (keeps it alive for the duration)
            try:
                publisher = EventPublisher(run.id, session)
                orchestrator = RunOrchestrator(session, publisher=publisher)
                await orchestrator.execute_run(run.id, state)
            except Exception as exc:
                logger.exception(
                    f"[Scheduler] Orchestrator failed for scheduled run {scheduled_id} "
                    f"(run {run_id}): {exc}"
                )

    except Exception as exc:
        logger.exception(f"[Scheduler] Error firing scheduled run {scheduled_id}: {exc}")


async def _dispatch_loop() -> None:
    """Polls for due scheduled runs every 60 seconds."""
    from src.infrastructure.database.connection import get_session_factory
    from src.infrastructure.database.flowpilot_models import ScheduledRunModel

    logger.info("[Scheduler] Dispatch loop started")

    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)

            async with get_session_factory()() as session:
                result = await session.execute(
                    select(ScheduledRunModel).where(
                        ScheduledRunModel.is_active.is_(True),
                        ScheduledRunModel.next_run_at <= now,
                    )
                )
                due = result.scalars().all()

                for scheduled in due:
                    logger.info(
                        f"[Scheduler] Scheduled run {scheduled.id} ({scheduled.name}) is due"
                    )

                    # Compute next fire time anchored to `now` so there's no drift
                    next_run = _compute_next(scheduled.cron_expression, after=now)
                    if next_run is None:
                        logger.warning(
                            f"[Scheduler] Could not compute next run for '{scheduled.name}' "
                            f"(id={scheduled.id}, cron='{scheduled.cron_expression}'). "
                            "Deactivating to prevent it from becoming permanently stuck."
                        )
                        await session.execute(
                            update(ScheduledRunModel)
                            .where(ScheduledRunModel.id == scheduled.id)
                            .values(last_run_at=now, is_active=False)
                        )
                        await session.commit()
                        continue

                    # Update timestamps first so we don't double-fire
                    await session.execute(
                        update(ScheduledRunModel)
                        .where(ScheduledRunModel.id == scheduled.id)
                        .values(last_run_at=now, next_run_at=next_run)
                    )
                    await session.commit()

                    # Fire asynchronously
                    asyncio.create_task(
                        _fire_run(scheduled.id, scheduled.business_id, scheduled.objective)
                    )

        except asyncio.CancelledError:
            logger.info("[Scheduler] Dispatch loop cancelled")
            break
        except Exception as exc:
            logger.exception(f"[Scheduler] Unexpected error in dispatch loop: {exc}")
            # Brief pause before retrying to avoid tight error loops
            await asyncio.sleep(10)


_expiry_task: Optional[asyncio.Task] = None
_reminder_task: Optional[asyncio.Task] = None

# How far ahead to look for upcoming runs (25 hours covers daily schedules
# without double-firing for crons shorter than daily).
_REMINDER_WINDOW_HOURS = 25

# Days before expiry on which to send warning emails (sent once per threshold crossed)
_EXPIRY_WARN_DAYS = {7, 3, 1}


async def _check_api_key_expiry() -> None:
    """Daily loop: email business owners about API keys expiring in 7, 3, or 1 day(s)."""
    from src.infrastructure.database.connection import get_session_factory
    from src.infrastructure.database.flowpilot_models import (
        ApiKeyModel,
        BusinessMemberModel,
        UserModel,
    )
    from sqlalchemy import select

    logger.info("[Scheduler] API key expiry check loop started")

    while True:
        try:
            await asyncio.sleep(86_400)  # run once per day
            now = datetime.now(timezone.utc)

            async with get_session_factory()() as session:
                # Load all active, non-revoked keys that have an expiry date
                result = await session.execute(
                    select(ApiKeyModel).where(
                        ApiKeyModel.revoked_at.is_(None),
                        ApiKeyModel.expires_at.isnot(None),
                        ApiKeyModel.expires_at > now,
                    )
                )
                keys = result.scalars().all()

                for key in keys:
                    days_left = (key.expires_at - now).days

                    if days_left not in _EXPIRY_WARN_DAYS:
                        continue

                    # Find the business owner's email
                    owner_result = await session.execute(
                        select(UserModel)
                        .join(
                            BusinessMemberModel,
                            BusinessMemberModel.user_id == UserModel.id,
                        )
                        .where(
                            BusinessMemberModel.business_id == key.business_id,
                            BusinessMemberModel.role == "owner",
                        )
                        .limit(1)
                    )
                    owner = owner_result.scalars().first()
                    if not owner or not owner.email:
                        continue

                    display_name = (
                        owner.display_name or owner.email.split("@")[0]
                    )

                    # In-app notification
                    from src.infrastructure.database.repositories.notification_repository import NotificationRepository as _NR
                    _notif_repo = _NR(session)
                    await _notif_repo.create(
                        user_id=owner.id,
                        business_id=key.business_id,
                        title=f"API key expiring in {days_left} day{'s' if days_left != 1 else ''}",
                        message=(
                            f'Your API key "{key.name}" ({key.key_prefix}…) expires in {days_left} day{"s" if days_left != 1 else ""}. '
                            "Rotate it in Developer Tools to avoid service disruption."
                        ),
                        type="warning",
                        resource_type="api_key",
                        resource_id=str(key.id),
                    )
                    await session.commit()

                    # Email
                    from src.services.email_service import send_api_key_expiry_warning
                    await send_api_key_expiry_warning(
                        to=owner.email,
                        display_name=display_name,
                        key_name=key.name,
                        key_prefix=key.key_prefix,
                        days_remaining=days_left,
                    )
                    logger.info(
                        "[Scheduler] Sent expiry warning for key %s (%d days left) to %s",
                        key.id,
                        days_left,
                        owner.email,
                    )

        except asyncio.CancelledError:
            logger.info("[Scheduler] API key expiry check loop cancelled")
            break
        except Exception as exc:
            logger.exception("[Scheduler] Error in API key expiry check: %s", exc)
            await asyncio.sleep(60)


async def _check_scheduled_reminders() -> None:
    """Hourly loop: notify business owners 24 hours before a scheduled run fires.

    Logic:
    - Find active scheduled runs whose next_run_at is within the next 25 hours.
    - Skip any where last_reminded_at already equals next_run_at (already notified
      for this occurrence).
    - Send an email + in-app notification to the business owner.
    - Mark last_reminded_at = next_run_at so we don't double-notify.
    """
    from src.infrastructure.database.connection import get_session_factory
    from src.infrastructure.database.flowpilot_models import (
        BusinessMemberModel,
        ScheduledRunModel,
        UserModel,
    )
    from sqlalchemy import select, update
    from src.infrastructure.database.repositories.notification_repository import NotificationRepository

    logger.info("[Scheduler] Day-before reminder loop started")

    while True:
        try:
            await asyncio.sleep(3_600)  # run once per hour
            now = datetime.now(timezone.utc)
            window_end = now + timedelta(hours=_REMINDER_WINDOW_HOURS)

            async with get_session_factory()() as session:
                result = await session.execute(
                    select(ScheduledRunModel).where(
                        ScheduledRunModel.is_active.is_(True),
                        ScheduledRunModel.next_run_at.isnot(None),
                        ScheduledRunModel.next_run_at > now,
                        ScheduledRunModel.next_run_at <= window_end,
                        # Only send once per occurrence
                        (
                            ScheduledRunModel.last_reminded_at.is_(None)
                            | (ScheduledRunModel.last_reminded_at != ScheduledRunModel.next_run_at)
                        ),
                    )
                )
                due = result.scalars().all()

                for scheduled in due:
                    try:
                        # Look up the business owner
                        owner_result = await session.execute(
                            select(BusinessMemberModel, UserModel)
                            .join(UserModel, BusinessMemberModel.user_id == UserModel.id)
                            .where(
                                BusinessMemberModel.business_id == scheduled.business_id,
                                BusinessMemberModel.role == "owner",
                                BusinessMemberModel.is_active.is_(True),
                            )
                            .limit(1)
                        )
                        owner_row = owner_result.first()
                        if not owner_row:
                            continue

                        _, owner_user = owner_row

                        fires_at_str = scheduled.next_run_at.strftime("%A, %d %B %Y at %I:%M %p UTC")

                        # In-app notification
                        notif_repo = NotificationRepository(session)
                        await notif_repo.create(
                            user_id=owner_user.id,
                            business_id=scheduled.business_id,
                            title="Scheduled run fires tomorrow",
                            message=(
                                f'"{scheduled.name}" is scheduled to run on {fires_at_str}. '
                                "Pause it if you need to make changes first."
                            ),
                            type="info",
                            resource_type="scheduled_run",
                            resource_id=str(scheduled.id),
                        )

                        # Email
                        from src.services.email_service import send_scheduled_run_reminder_email
                        asyncio.create_task(
                            send_scheduled_run_reminder_email(
                                to=owner_user.email,
                                display_name=owner_user.display_name or owner_user.email,
                                schedule_name=scheduled.name,
                                objective=scheduled.objective,
                                fires_at=fires_at_str,
                                frequency_label=scheduled.frequency_label,
                                scheduled_run_id=str(scheduled.id),
                            )
                        )

                        # Mark as reminded for this occurrence
                        await session.execute(
                            update(ScheduledRunModel)
                            .where(ScheduledRunModel.id == scheduled.id)
                            .values(last_reminded_at=scheduled.next_run_at)
                        )

                        logger.info(
                            "[Scheduler] Sent day-before reminder for '%s' (id=%s) firing at %s",
                            scheduled.name,
                            scheduled.id,
                            fires_at_str,
                        )
                    except Exception as exc:
                        logger.exception(
                            "[Scheduler] Error sending reminder for scheduled run %s: %s",
                            scheduled.id,
                            exc,
                        )

                await session.commit()

        except asyncio.CancelledError:
            logger.info("[Scheduler] Day-before reminder loop cancelled")
            break
        except Exception as exc:
            logger.exception("[Scheduler] Unexpected error in reminder loop: %s", exc)
            await asyncio.sleep(60)


_2fa_reminder_task: Optional[asyncio.Task] = None

# Send the warning when grace_until is within this many minutes of expiry.
_2FA_WARN_MINUTES = 20
# Poll every 5 minutes so we don't miss the window.
_2FA_POLL_SECONDS = 300


async def _check_2fa_grace_expiry() -> None:
    """Polls every 5 minutes for members whose 2FA grace period expires within 20 minutes.

    Sends a single reminder email per user (tracked via a set of already-notified IDs
    in memory — the process only sends once per restart, which is fine since the email
    is only useful during the window).
    """
    from src.infrastructure.database.connection import get_session_factory
    from src.infrastructure.database.flowpilot_models import UserModel
    from sqlalchemy import select

    logger.info("[Scheduler] 2FA grace-expiry reminder loop started")
    already_notified: set = set()

    while True:
        try:
            await asyncio.sleep(_2FA_POLL_SECONDS)
            now = datetime.now(timezone.utc)
            warn_before = now + timedelta(minutes=_2FA_WARN_MINUTES)

            async with get_session_factory()() as session:
                # Find users who haven't set up 2FA, have a grace deadline,
                # and that deadline falls within the next 20 minutes.
                result = await session.execute(
                    select(UserModel).where(
                        UserModel.totp_enabled_at.is_(None),
                        UserModel.totp_grace_until.isnot(None),
                        UserModel.totp_grace_until > now,       # not yet expired
                        UserModel.totp_grace_until <= warn_before,  # expiring soon
                        UserModel.is_active.is_(True),
                    )
                )
                users = result.scalars().all()

                for user in users:
                    if user.id in already_notified:
                        continue

                    minutes_left = max(
                        1,
                        int((user.totp_grace_until - now).total_seconds() / 60),
                    )

                    from src.services.email_service import send_2fa_grace_expiring_email
                    sent = await send_2fa_grace_expiring_email(
                        to=user.email,
                        display_name=user.display_name or user.email,
                        minutes_left=minutes_left,
                    )

                    if sent:
                        already_notified.add(user.id)
                        logger.info(
                            "[Scheduler] Sent 2FA expiry warning to %s (%d min left)",
                            user.email,
                            minutes_left,
                        )
                    else:
                        logger.warning(
                            "[Scheduler] Failed to send 2FA expiry warning to %s",
                            user.email,
                        )

        except asyncio.CancelledError:
            logger.info("[Scheduler] 2FA grace-expiry reminder loop cancelled")
            break
        except Exception as exc:
            logger.exception("[Scheduler] Error in 2FA grace-expiry check: %s", exc)
            await asyncio.sleep(60)


def start_scheduler() -> None:
    """Start the background dispatch loop. Call once from app lifespan."""
    global _task, _expiry_task, _reminder_task, _2fa_reminder_task
    if _task is None or _task.done():
        _task = asyncio.create_task(_dispatch_loop())
        logger.info("[Scheduler] Background task created")
    if _expiry_task is None or _expiry_task.done():
        _expiry_task = asyncio.create_task(_check_api_key_expiry())
        logger.info("[Scheduler] API key expiry task created")
    if _reminder_task is None or _reminder_task.done():
        _reminder_task = asyncio.create_task(_check_scheduled_reminders())
        logger.info("[Scheduler] Day-before reminder task created")
    if _2fa_reminder_task is None or _2fa_reminder_task.done():
        _2fa_reminder_task = asyncio.create_task(_check_2fa_grace_expiry())
        logger.info("[Scheduler] 2FA grace-expiry reminder task created")


def stop_scheduler() -> None:
    """Cancel the background dispatch loop. Call from app shutdown."""
    global _task, _expiry_task, _reminder_task, _2fa_reminder_task
    if _task and not _task.done():
        _task.cancel()
        logger.info("[Scheduler] Background task cancelled")
    if _expiry_task and not _expiry_task.done():
        _expiry_task.cancel()
        logger.info("[Scheduler] API key expiry task cancelled")
    if _reminder_task and not _reminder_task.done():
        _reminder_task.cancel()
        logger.info("[Scheduler] Day-before reminder task cancelled")
    if _2fa_reminder_task and not _2fa_reminder_task.done():
        _2fa_reminder_task.cancel()
        logger.info("[Scheduler] 2FA grace-expiry reminder task cancelled")

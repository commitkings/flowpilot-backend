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
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None


def _compute_next(cron_expr: str, after: Optional[datetime] = None) -> Optional[datetime]:
    try:
        from croniter import croniter
        base = after or datetime.now(timezone.utc)
        return croniter(cron_expr, base).get_next(datetime).replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _fire_run(scheduled_id: uuid.UUID, business_id: uuid.UUID, objective: str) -> None:
    """Create and kick off a new AgentRunModel for the scheduled run."""
    try:
        from src.infrastructure.database.connection import get_session_factory
        from src.infrastructure.database.flowpilot_models import (
            AgentRunModel,
            BusinessModel,
        )
        from src.config.settings import Settings

        async with get_session_factory()() as session:
            # Look up the business to get merchant_id
            biz_result = await session.execute(
                select(BusinessModel).where(BusinessModel.id == business_id)
            )
            business = biz_result.scalars().first()
            if not business:
                logger.warning(f"[Scheduler] Business {business_id} not found for scheduled run {scheduled_id}")
                return

            run = AgentRunModel(
                business_id=business_id,
                created_by=None,
                objective=objective,
                merchant_id=getattr(business, "merchant_id", "") or "",
                status="pending",
                risk_tolerance=0.35,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)

            logger.info(
                f"[Scheduler] Fired scheduled run {scheduled_id} → new run {run.id}"
            )

            # Kick off orchestration in background (don't await — let it run independently)
            try:
                from src.agents.orchestrator import RunOrchestrator
                from src.agents.state import AgentState
                from src.infrastructure.database.repositories import (
                    AuditRepository,
                    CandidateRepository,
                    RunRepository,
                )

                run_repo = RunRepository(session)
                candidate_repo = CandidateRepository(session)
                audit_repo = AuditRepository(session)
                orchestrator = RunOrchestrator(
                    run_repo=run_repo,
                    candidate_repo=candidate_repo,
                    audit_repo=audit_repo,
                )
                state = AgentState(run_id=str(run.id), business_id=str(business_id))
                asyncio.create_task(orchestrator.run(state))
            except Exception as exc:
                logger.warning(f"[Scheduler] Could not start orchestrator for run {run.id}: {exc}")

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

                    # Compute next fire time before we fire
                    next_run = _compute_next(scheduled.cron_expression)

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


def start_scheduler() -> None:
    """Start the background dispatch loop. Call once from app lifespan."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_dispatch_loop())
    logger.info("[Scheduler] Background task created")


def stop_scheduler() -> None:
    """Cancel the background dispatch loop. Call from app shutdown."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        logger.info("[Scheduler] Background task cancelled")

"""Agent worker — consumes run jobs from Redis and executes the AI pipeline.

Queue: fp:agent:jobs (FIFO via LPUSH / BRPOP)
Each job: {"run_id": "<uuid>", "date_from": "...", "date_to": "...", "objective": "..."}

Failure contract:
- If the orchestrator raises, the run is marked "failed" and the worker continues.
- If Redis drops, the worker reconnects and retries with exponential back-off.
- Inline execution fallback in create_run() handles the case where Redis is down
  at enqueue time, so no job is ever silently lost.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

logger = logging.getLogger(__name__)

_RECONNECT_DELAYS = [1, 2, 5, 10, 30]  # seconds


async def _process_job(session_factory, job: dict) -> None:
    """Execute one run job inside its own DB session."""
    run_id_str = job.get("run_id")
    if not run_id_str:
        logger.warning("agent_worker: job missing run_id, skipping: %s", job)
        return

    run_id = uuid.UUID(run_id_str)

    async with session_factory() as session:
        try:
            from src.infrastructure.database.repositories import (
                CandidateRepository,
                RunRepository,
            )
            run_repo = RunRepository(session)
            candidate_repo = CandidateRepository(session)

            run = await run_repo.get_by_id(run_id)
            if run is None:
                logger.warning("agent_worker: run %s not found, skipping", run_id)
                return

            # Skip runs that are already past pending/planning (e.g. picked up by
            # inline fallback while Redis was intermittently available)
            if run.status not in ("pending", "planning"):
                logger.info("agent_worker: run %s already in status=%s, skipping", run_id, run.status)
                return

            # Load persisted candidates (already in DB from create_run)
            candidates = await candidate_repo.get_by_run(run_id)
            candidate_dicts = [
                {
                    "candidate_id": str(c.id),
                    "institution_code": c.institution_code,
                    "beneficiary_name": c.beneficiary_name,
                    "account_number": c.account_number,
                    "beneficiary_email": getattr(c, "beneficiary_email", None),
                    "amount": float(c.amount),
                    "currency": c.currency,
                    "purpose": c.purpose,
                }
                for c in candidates
            ]

            state = {
                "run_id": run_id_str,
                "business_id": str(run.business_id),
                "objective": run.objective,
                "constraints": run.constraints,
                "date_from": job.get("date_from"),
                "date_to": job.get("date_to"),
                "risk_tolerance": float(run.risk_tolerance),
                "budget_cap": float(run.budget_cap) if run.budget_cap is not None else None,
                "merchant_id": run.merchant_id,
                "plan_steps": [],
                "transactions": [],
                "reconciled_ledger": {},
                "unresolved_references": [],
                "resolved_references": [],
                "scored_candidates": candidate_dicts,
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

            logger.info("agent_worker: executing run %s (%s)", run_id, run.objective[:60])

            from src.agents.event_publisher import EventPublisher
            from src.agents.orchestrator import RunOrchestrator

            publisher = EventPublisher(run.id, session)
            orchestrator = RunOrchestrator(session, publisher=publisher)
            state = await orchestrator.execute_run(run.id, state)

            await _handle_post_execution(session, run, state, run_id_str)

        except Exception as exc:
            logger.exception("agent_worker: run %s failed: %s", run_id, exc)
            try:
                await session.rollback()
                from src.infrastructure.database.repositories import RunRepository as _RR
                async with session_factory() as _s:
                    await _RR(_s).update_status(run_id, "failed", str(exc)[:500])
                    await _s.commit()
            except Exception:
                pass


async def _handle_post_execution(session, run, state: dict, run_id_str: str) -> None:
    """Notify approvers or creator depending on pipeline outcome."""
    from sqlalchemy import select as _sel
    from src.config.settings import Settings
    from src.infrastructure.database.flowpilot_models import BusinessMemberModel, UserModel
    from src.infrastructure.database.repositories.notification_repository import NotificationRepository
    from src.services.email_service import (
        check_notification_pref,
        send_run_awaiting_approval_email,
    )
    from src.services.webhook_dispatcher import dispatch_event

    current_step = state.get("current_step")
    business_id = run.business_id
    objective = run.objective or ""
    notif_repo = NotificationRepository(session)

    if current_step == "awaiting_approval":
        # ── Fire approval.requested webhook ──────────────────────────────────
        try:
            _candidates = state.get("scored_candidates", [])
            _total = sum(float(c.get("amount", 0)) for c in _candidates)
            _breakdown: dict = {"allow": 0, "review": 0, "block": 0}
            _flagged = []
            for _c in _candidates:
                _d = _c.get("risk_decision", "allow")
                _breakdown[_d] = _breakdown.get(_d, 0) + 1
                if _d in ("review", "block"):
                    _flagged.append({
                        "candidate_id": _c.get("candidate_id"),
                        "beneficiary_name": _c.get("beneficiary_name"),
                        "amount": float(_c.get("amount", 0)),
                        "risk_score": float(_c.get("risk_score", 0)),
                        "risk_decision": _d,
                    })
            asyncio.create_task(dispatch_event(business_id, "approval.requested", {
                "run_id": run_id_str,
                "objective": objective,
                "candidate_count": len(_candidates),
                "total_payout_amount": _total,
                "currency": "NGN",
                "risk_breakdown": _breakdown,
                "flagged_candidates": _flagged,
                "approval_url": f"{Settings.FRONTEND_URL}/runs/{run_id_str}",
            }))
        except Exception as _wh:
            logger.warning("agent_worker: webhook dispatch failed: %s", _wh)

        # ── Notify all active approvers / owners ──────────────────────────────
        try:
            rows = (await session.execute(
                _sel(BusinessMemberModel, UserModel)
                .join(UserModel, BusinessMemberModel.user_id == UserModel.id)
                .where(
                    BusinessMemberModel.business_id == business_id,
                    BusinessMemberModel.role.in_(["owner", "approver"]),
                    BusinessMemberModel.is_active.is_(True),
                )
            )).all()

            cand_count = len(state.get("scored_candidates", []))

            # Honour assigned approver if set
            if run.assigned_to_id:
                targets = [(m, u) for m, u in rows if m.user_id == run.assigned_to_id]
                if not targets:
                    targets = rows[:1]
            elif len(rows) <= 1:
                targets = rows
            else:
                non_creator = [(m, u) for m, u in rows if m.user_id != run.created_by]
                approvers = [(m, u) for m, u in non_creator if m.role == "approver"]
                targets = (approvers or non_creator or rows)[:1]

            for _m, _u in targets:
                if check_notification_pref(_u, "payout_updates"):
                    await send_run_awaiting_approval_email(
                        to=_u.email,
                        run_id=run_id_str,
                        objective=objective,
                        candidate_count=cand_count,
                        approver_name=_u.display_name or _u.email,
                        frontend_url=Settings.FRONTEND_URL,
                    )
                await notif_repo.create(
                    user_id=_u.id,
                    business_id=business_id,
                    title="Approval needed",
                    message=f'{cand_count} candidate{"s" if cand_count != 1 else ""} need your review on run "{objective[:50]}".',
                    type="warning",
                    resource_type="run",
                    resource_id=run_id_str,
                )
            await session.commit()
        except Exception as _n:
            logger.warning("agent_worker: approver notification failed: %s", _n)

    elif state.get("error"):
        if run.created_by:
            try:
                await notif_repo.create(
                    user_id=run.created_by,
                    business_id=business_id,
                    title="Run failed",
                    message=f'Your run "{objective[:50]}" encountered an error.',
                    type="error",
                    resource_type="run",
                    resource_id=run_id_str,
                )
                await session.commit()
            except Exception:
                pass

    else:
        if run.created_by:
            try:
                await notif_repo.create(
                    user_id=run.created_by,
                    business_id=business_id,
                    title="Run completed",
                    message=f'Your run "{objective[:50]}" completed successfully.',
                    type="success",
                    resource_type="run",
                    resource_id=run_id_str,
                )
                await session.commit()
            except Exception:
                pass


async def _worker_loop(session_factory) -> None:
    """Main loop: connect to Redis, BRPOP jobs, process them."""
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        logger.warning("agent_worker: REDIS_URL not set — worker idle (no queue)")
        while True:
            await asyncio.sleep(60)

    import redis.asyncio as redis
    from src.infrastructure.queue.agent_queue import dequeue_run

    delay_idx = 0
    r = None

    while True:
        try:
            if r is None:
                r = redis.from_url(url, decode_responses=True)
                logger.info("agent_worker: connected to Redis, listening on fp:agent:jobs")
                delay_idx = 0

            job = await dequeue_run(r, timeout=5)
            if job is None:
                continue  # timeout — loop again

            logger.info("agent_worker: dequeued job run_id=%s", job.get("run_id"))
            await _process_job(session_factory, job)

        except Exception as exc:
            delay = _RECONNECT_DELAYS[min(delay_idx, len(_RECONNECT_DELAYS) - 1)]
            logger.error("agent_worker: error in main loop (%s), reconnecting in %ds", exc, delay)
            if r is not None:
                try:
                    await r.close()
                except Exception:
                    pass
                r = None
            delay_idx += 1
            await asyncio.sleep(delay)


async def _run() -> None:
    from src.infrastructure.database.connection import close_db, get_session_factory, init_db
    await init_db()
    session_factory = get_session_factory()
    logger.info("agent_worker: started")
    try:
        await _worker_loop(session_factory)
    finally:
        await close_db()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()

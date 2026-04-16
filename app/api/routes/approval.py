import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.routes.runs import _parse_uuid, _running_states
from src.services.email_service import send_run_completed_email
from src.infrastructure.database.repositories.notification_repository import NotificationRepository
from src.agents.orchestrator import RunOrchestrator, _map_transactions
from src.agents.event_publisher import EventPublisher
from src.agents.state import AgentState
from src.config.settings import Settings
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    AgentRunModel,
    BusinessMemberModel,
    BusinessModel,
    KycLimitTrackerModel,
    UserModel,
)
from src.infrastructure.database.repositories import (
    AuditRepository,
    BatchRepository,
    CandidateRepository,
    ConversationRepository,
    InstitutionRepository,
    RunRepository,
    TransactionRepository,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class ApprovalRequest(BaseModel):
    candidate_ids: list[str]


class RejectionRequest(BaseModel):
    candidate_ids: list[str]
    reason: Optional[str] = None


class AssignApproverRequest(BaseModel):
    user_id: str


class UpdateCandidateRequest(BaseModel):
    amount: Optional[float] = None
    beneficiary_name: Optional[str] = None
    account_number: Optional[str] = None
    institution_code: Optional[str] = None


def _parse_uuid_list(values: list[str], field_name: str) -> list[uuid.UUID]:
    try:
        return [uuid.UUID(value) for value in values]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _serialize_candidate(candidate) -> dict:
    return {
        "id": str(candidate.id),
        "run_id": str(candidate.run_id),
        "institution_code": candidate.institution_code,
        "beneficiary_name": candidate.beneficiary_name,
        "account_number": candidate.account_number,
        "amount": float(candidate.amount),
        "currency": candidate.currency,
        "purpose": candidate.purpose,
        "risk_score": float(candidate.risk_score) if candidate.risk_score is not None else None,
        "risk_reasons": candidate.risk_reasons,
        "risk_decision": candidate.risk_decision,
        "lookup_account_name": candidate.lookup_account_name,
        "lookup_match_score": float(candidate.lookup_match_score) if candidate.lookup_match_score is not None else None,
        "approval_status": candidate.approval_status,
        "execution_status": candidate.execution_status,
        "approved_by": str(candidate.approved_by) if candidate.approved_by else None,
        "approved_at": candidate.approved_at.isoformat() if candidate.approved_at else None,
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
    }


# ------------------------------------------------------------------
# DB state reconstruction (for crash recovery / server restart)
# ------------------------------------------------------------------


def _serialize_plan_step(plan_step) -> dict:
    return {
        "step_id": str(plan_step.id),
        "agent_type": plan_step.agent_type,
        "order": plan_step.step_order,
        "description": plan_step.description,
        "status": plan_step.status,
    }


def _serialize_transaction(transaction) -> dict:
    return {
        "transactionReference": transaction.interswitch_ref,
        "amount": float(transaction.amount),
        "currency": transaction.currency,
        "direction": transaction.direction,
        "status": transaction.status,
        "channel": transaction.channel,
        "narration": transaction.narration,
        "timestamp": (
            transaction.transaction_timestamp.isoformat()
            if transaction.transaction_timestamp
            else None
        ),
        "settlementDate": (
            transaction.settlement_date.isoformat()
            if transaction.settlement_date
            else None
        ),
        "counterpartyName": transaction.counterparty_name,
        "counterpartyBank": transaction.counterparty_bank,
        "hasAnomaly": transaction.has_anomaly,
        "anomalyCount": transaction.anomaly_count,
    }


def _serialize_scored_candidate(candidate) -> dict:
    return {
        "candidate_id": str(candidate.id),
        "institution_code": candidate.institution_code,
        "beneficiary_name": candidate.beneficiary_name,
        "account_number": candidate.account_number,
        "amount": float(candidate.amount),
        "currency": candidate.currency,
        "purpose": candidate.purpose,
        "risk_score": float(candidate.risk_score) if candidate.risk_score is not None else None,
        "risk_reasons": candidate.risk_reasons or [],
        "risk_decision": candidate.risk_decision,
        "approval_status": candidate.approval_status,
        "execution_status": candidate.execution_status,
        "client_reference": candidate.client_reference,
        "provider_reference": candidate.provider_reference,
    }


def _build_reconciled_ledger(transactions: list[dict]) -> dict:
    ledger = {
        "total_inflow": 0.0,
        "total_outflow": 0.0,
        "pending_amount": 0.0,
        "failed_amount": 0.0,
        "success_count": 0,
        "pending_count": 0,
        "failed_count": 0,
        "reversed_count": 0,
    }
    for transaction in transactions:
        amount = transaction.get("amount", 0.0)
        status = transaction.get("status")
        if status == "SUCCESS":
            ledger["total_inflow"] += amount
            ledger["success_count"] += 1
        elif status == "PENDING":
            ledger["pending_amount"] += amount
            ledger["pending_count"] += 1
        elif status == "FAILED":
            ledger["failed_amount"] += amount
            ledger["failed_count"] += 1
        elif status == "REVERSED":
            ledger["reversed_count"] += 1
    return ledger


async def _reconstruct_state_from_db(
    session: AsyncSession, run_id: uuid.UUID
) -> Optional[AgentState]:
    """Rebuild full AgentState from DB for crash recovery."""
    run_repo = RunRepository(session)
    run = await run_repo.get_by_id(run_id)
    if run is None:
        return None

    plan_steps = [_serialize_plan_step(step) for step in run.run_steps]
    transactions = [_serialize_transaction(txn) for txn in run.reconciled_transactions]
    scored_candidates = [
        _serialize_scored_candidate(c) for c in run.payout_candidates
    ]
    approved_ids = [
        str(c.id) for c in run.payout_candidates if c.approval_status == "approved"
    ]
    rejected_ids = [
        str(c.id) for c in run.payout_candidates if c.approval_status == "rejected"
    ]
    unresolved = [
        t["transactionReference"]
        for t in transactions
        if t.get("status") == "PENDING" and t.get("transactionReference")
    ]

    return {
        "run_id": str(run.id),
        "business_id": str(run.business_id),
        "objective": run.objective,
        "constraints": run.constraints,
        "risk_tolerance": float(run.risk_tolerance),
        "budget_cap": float(run.budget_cap) if run.budget_cap is not None else None,
        "merchant_id": run.merchant_id,
        "plan_steps": plan_steps,
        "transactions": transactions,
        "reconciled_ledger": _build_reconciled_ledger(transactions),
        "unresolved_references": unresolved,
        "resolved_references": [],
        "scored_candidates": scored_candidates,
        "forecast": None,
        "candidate_lookup_results": [],
        "candidate_execution_results": [],
        "batch_details": None,
        "approved_candidate_ids": approved_ids,
        "rejected_candidate_ids": rejected_ids,
        "audit_report": None,
        "current_step": "approved",
        "error": run.error_message,
        "audit_entries": [],
        "reasoning_log": [],
    }


# ------------------------------------------------------------------
# Conversation sync after approval-driven run completion
# ------------------------------------------------------------------


async def _sync_conversation_after_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    final_status: str,
    error: str | None,
) -> None:
    """Update the conversation linked to this run with a terminal status and message."""
    try:
        conv_repo = ConversationRepository(session)
        conv = await conv_repo.get_by_run_id(run_id)
        if conv is None:
            logger.warning(f"Run {run_id}: no conversation found to sync")
            return

        # Craft a user-friendly completion message
        if final_status == "completed":
            summary = (
                "Your payout run completed successfully!\n\n"
                "All approved transactions have been processed. "
                "You can view the detailed results in the run dashboard."
            )
        elif final_status == "failed":
            summary = "Your payout run failed."
            if error:
                summary += f"\n\nError: {error}"
        else:
            summary = f"Run finished with status: {final_status}."
            if error:
                summary += f"\n\nError: {error}"

        # Use "assistant" role so it displays as an AI message in the chat UI
        await conv_repo.update_conversation(conv.id, status="completed")
        await conv_repo.add_message(conv.id, role="assistant", content=summary)
        await session.commit()
        logger.info(f"Run {run_id}: conversation {conv.id} synced to status=completed")
    except Exception as exc:
        logger.warning(f"Run {run_id}: failed to sync conversation after approval: {exc}", exc_info=True)


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------


@router.get("/runs/{run_id}/candidates")
async def get_candidates(
    run_id: str,
    approval_status: Optional[str] = None,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)

    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    from sqlalchemy import select as _select_mem
    _mem_q = await session.execute(
        _select_mem(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _mem_q.scalars().first():
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    candidates = await candidate_repo.get_by_run(run_uuid, approval_status=approval_status)

    return {
        "run_id": run_id,
        "total": len(candidates),
        "candidates": [_serialize_candidate(c) for c in candidates],
        "status": run.status,
    }


@router.post("/runs/{run_id}/approve")
async def approve_candidates(
    run_id: str,
    request: ApprovalRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
):
    from decimal import Decimal
    from sqlalchemy import select as _sa

    run_uuid = _parse_uuid(run_id, "run_id")
    candidate_ids = _parse_uuid_list(request.candidate_ids, "candidate_ids")
    if not candidate_ids:
        raise HTTPException(status_code=400, detail="candidate_ids must not be empty")

    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)
    transaction_repo = TransactionRepository(session)
    batch_repo = BatchRepository(session)

    # ── 1. Load run (needed for all pre-CAS gate checks) ─────────────────
    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # In simulated payout modes, recover stale failed approvals after a backend fix.
    if run.status == "failed" and Settings.is_payout_simulated():
        existing_batches = await batch_repo.get_by_run(run_uuid)
        if not existing_batches:
            await run_repo.update_status(run_uuid, "awaiting_approval", None)
            await session.commit()
            run = await run_repo.get_by_id(run_uuid)

    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Run is not awaiting approval (status: {run.status})",
        )

    # ── 2. Authorization: caller must be an active owner/approver in this business ──
    _biz_mem_q = await session.execute(
        _sa(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    _current_membership = _biz_mem_q.scalars().first()
    if not _current_membership or _current_membership.role not in ("owner", "approver"):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to approve runs in this organisation.",
        )

    # If a specific approver is assigned, only they (or an owner) may proceed
    if run.assigned_to_id and str(run.assigned_to_id) != str(current_user.id):
        if _current_membership.role != "owner":
            raise HTTPException(
                status_code=403,
                detail="Only the assigned approver can approve this run.",
            )

    # ── 3. Load candidates to validate budget cap and compute debit amount ─
    all_run_candidates = await candidate_repo.get_by_run(run_uuid)
    candidates_by_id = {str(c.id): c for c in all_run_candidates}
    selected_candidates = [
        candidates_by_id[str(cid)] for cid in candidate_ids if str(cid) in candidates_by_id
    ]
    total_approved = sum(float(c.amount) for c in selected_candidates)

    if run.budget_cap is not None and total_approved > float(run.budget_cap):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Total approved amount (₦{total_approved:,.2f}) exceeds the run budget cap "
                f"(₦{float(run.budget_cap):,.2f}). Reduce the selection or increase the budget cap."
            ),
        )

    # ── 3b. KYC payout limits — single transaction cap + monthly cap ────────
    from decimal import Decimal as _D
    from datetime import date as _date
    from src.config.kyc_limits import get_limits as _get_limits, SUPPORT_EMAIL as _SUPPORT_EMAIL
    from sqlalchemy import select as _sa_sel

    _biz_result = await session.execute(
        _sa_sel(BusinessModel).where(BusinessModel.id == run.business_id)
    )
    _biz = _biz_result.scalar_one_or_none()
    if _biz:
        _account_type = getattr(_biz, "account_type", "business") or "business"
        _kyc_level = getattr(_biz, "kyc_level", 0) or 0
        _limits = _get_limits(_account_type, _kyc_level)

        if _limits:
            # Single transaction cap
            for c in selected_candidates:
                if _D(str(c.amount)) > _limits["single"]:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Payout to {c.beneficiary_name} (₦{float(c.amount):,.2f}) exceeds your "
                            f"single-transaction limit of ₦{float(_limits['single']):,.2f}. "
                            f"Upgrade your KYC level or contact {_SUPPORT_EMAIL} for a higher limit."
                        ),
                    )

            # Monthly cap: get or create tracker, reset if new month
            _today = _date.today()
            _month_start = _today.replace(day=1)
            _tracker_result = await session.execute(
                _sa_sel(KycLimitTrackerModel).where(
                    KycLimitTrackerModel.business_id == run.business_id
                )
            )
            _tracker = _tracker_result.scalar_one_or_none()

            if _tracker is None:
                _tracker = KycLimitTrackerModel(
                    business_id=run.business_id,
                    monthly_payout_used=_D("0.00"),
                    month_start=_month_start,
                )
                session.add(_tracker)
                await session.flush()
            elif _tracker.month_start < _month_start:
                # New month — reset counter
                _tracker.monthly_payout_used = _D("0.00")
                _tracker.month_start = _month_start
                await session.flush()

            _projected = _tracker.monthly_payout_used + _D(str(total_approved))
            if _projected > _limits["monthly"]:
                _remaining = max(_D("0"), _limits["monthly"] - _tracker.monthly_payout_used)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"This payout (₦{total_approved:,.2f}) would exceed your monthly limit of "
                        f"₦{float(_limits['monthly']):,.2f}. You have ₦{float(_remaining):,.2f} remaining "
                        f"this month. Upgrade your KYC level or contact {_SUPPORT_EMAIL} for a higher limit."
                    ),
                )
        elif _kyc_level == 0:
            raise HTTPException(
                status_code=403,
                detail="Complete KYC verification to execute payouts.",
                headers={"X-KYC-Status": "not_submitted"},
            )

    # ── 4. Atomic CAS: awaiting_approval → executing ───────────────────────
    acquired = await run_repo.transition_status(run_uuid, "awaiting_approval", "executing")
    if not acquired:
        run_fresh = await run_repo.get_by_id(run_uuid)
        raise HTTPException(
            status_code=409,
            detail=f"Run is not awaiting approval (status: {run_fresh.status if run_fresh else 'unknown'})",
        )

    # ── 5. Wallet debit — debit approved total + 0.2 % platform fee ─────────
    _PLATFORM_FEE_RATE = Decimal("0.002")
    _wallet_balance_after: float | None = None
    if total_approved > 0:
        from src.infrastructure.database.repositories.wallet_repository import (
            WalletRepository as _WalletRepo,
            InsufficientBalanceError as _InsufficientBalance,
            LOW_BALANCE_THRESHOLD as _LOW_THRESHOLD,
        )
        _wallet_repo = _WalletRepo(session)
        _total_decimal = Decimal(str(total_approved))
        _fee_amount = (_total_decimal * _PLATFORM_FEE_RATE).quantize(Decimal("0.01"))
        try:
            # Debit payout amount
            _debit_tx, _ = await _wallet_repo.debit(
                business_id=run.business_id,
                amount=_total_decimal,
                reference=f"run_exec_{run_id}",
                description=f"Payout execution: {run.objective[:80]}",
                run_id=run_uuid,
            )
            _wallet_balance_after = float(_debit_tx.balance_after)

            # Debit platform fee (separate ledger line)
            if _fee_amount > 0:
                _fee_tx, _ = await _wallet_repo.debit(
                    business_id=run.business_id,
                    amount=_fee_amount,
                    reference=f"platform_fee_{run_id}",
                    description=f"Platform fee (0.2%): {run.objective[:60]}",
                    run_id=run_uuid,
                )
                _wallet_balance_after = float(_fee_tx.balance_after)

            # Persist fee on the run
            run.platform_fee_rate = _PLATFORM_FEE_RATE
            run.platform_fee_amount = _fee_amount

            # Update monthly KYC limit tracker
            try:
                from sqlalchemy import select as _sa_lim
                from src.infrastructure.database.flowpilot_models import KycLimitTrackerModel as _KycTracker
                from datetime import datetime as _dt2, timezone as _tz2, date as _d2
                _t2 = await session.execute(
                    _sa_lim(_KycTracker).where(_KycTracker.business_id == run.business_id)
                )
                _tracker2 = _t2.scalar_one_or_none()
                if _tracker2 is None:
                    # Shouldn't happen (created during pre-check), but create defensively
                    _tracker2 = _KycTracker(
                        business_id=run.business_id,
                        monthly_payout_used=_total_decimal,
                        month_start=_date.today().replace(day=1),
                    )
                    session.add(_tracker2)
                else:
                    _tracker2.monthly_payout_used += _total_decimal
                    _tracker2.updated_at = _dt2.now(_tz2.utc)
            except Exception as _lim_exc:
                logger.warning("Could not update KYC limit tracker: %s", _lim_exc)

        except _InsufficientBalance as exc:
            # Revert status so the approver can try again after topping up
            await run_repo.update_status(run_uuid, "awaiting_approval", None)
            await session.commit()
            raise HTTPException(status_code=402, detail=str(exc))
        except (ValueError, Exception) as exc:
            await run_repo.update_status(run_uuid, "awaiting_approval", None)
            await session.commit()
            raise HTTPException(
                status_code=402,
                detail="Your organisation wallet has no balance. Please top up before executing.",
            )

    await session.commit()

    # Low-balance alert after successful debit
    if _wallet_balance_after is not None:
        _LOW_THRESHOLD_VAL = Decimal("50000.00")
        if Decimal(str(_wallet_balance_after)) < _LOW_THRESHOLD_VAL:
            try:
                import asyncio as _asyncio2
                from src.services.email_service import send_wallet_low_balance_email as _send_lb
                _owner_result = await session.execute(
                    _sa(BusinessMemberModel, UserModel)
                    .join(UserModel, BusinessMemberModel.user_id == UserModel.id)
                    .where(
                        BusinessMemberModel.business_id == run.business_id,
                        BusinessMemberModel.role == "owner",
                        BusinessMemberModel.is_active.is_(True),
                    )
                    .limit(1)
                )
                _owner_row = _owner_result.first()
                if _owner_row:
                    _, _owner_user = _owner_row
                    _notif_repo = NotificationRepository(session)
                    await _notif_repo.create(
                        user_id=_owner_user.id,
                        business_id=run.business_id,
                        title="Wallet balance is low",
                        message=(
                            f"Your wallet balance (₦{_wallet_balance_after:,.2f}) is below the "
                            f"₦{float(_LOW_THRESHOLD_VAL):,.2f} threshold. Top up to avoid disruptions."
                        ),
                        type="warning",
                        resource_type="wallet",
                    )
                    await session.commit()
                    _asyncio2.create_task(_send_lb(
                        to=_owner_user.email,
                        display_name=_owner_user.display_name or _owner_user.email,
                        balance=_wallet_balance_after,
                        threshold=float(_LOW_THRESHOLD_VAL),
                    ))
            except Exception as _lb_exc:
                logger.warning("[Wallet] Low-balance alert failed after approval debit: %s", _lb_exc)

    # Idempotency guard: reject if this run already has a payout batch
    existing_batches = await batch_repo.get_by_run(run_uuid)
    if existing_batches:
        raise HTTPException(
            status_code=409,
            detail="Run already has a payout batch — cannot re-execute",
        )

    run = await run_repo.get_by_id(run_uuid)

    # Get or reconstruct state
    state = _running_states.get(run_id)
    if state is None:
        state = await _reconstruct_state_from_db(session, run_uuid)
        if state is None:
            raise HTTPException(status_code=404, detail="Run not found")
    state["error"] = None

    # Approve candidates in DB (candidate_ids already validated above)
    approved_count = await candidate_repo.approve(candidate_ids, current_user.id, run_uuid)

    # Stamp the run itself with who approved it and when
    from datetime import datetime as _dt, timezone as _tz
    run.approved_by = current_user.id
    run.approved_at = _dt.now(_tz.utc)
    await session.flush()

    # Audit log: approval action
    audit_repo = AuditRepository(session)
    await audit_repo.append(
        run_id=run_uuid,
        action="candidates_approved",
        detail={
            "candidate_ids": [str(cid) for cid in candidate_ids],
            "approved_count": approved_count,
            "approved_by": str(current_user.id),
        },
    )
    await session.commit()

    # Update in-memory state
    existing_approved = set(state.get("approved_candidate_ids", []))
    existing_approved.update(str(cid) for cid in candidate_ids)
    state["approved_candidate_ids"] = list(existing_approved)
    state["current_step"] = "approved"

    logger.info(f"Run {run_id}: approved {approved_count} candidates, resuming execution")

    try:
        # Re-persist transactions (safe: ON CONFLICT DO NOTHING)
        transactions = state.get("transactions", [])
        if transactions:
            business_id = uuid.UUID(state["business_id"]) if state.get("business_id") else run.business_id
            await transaction_repo.create_batch(
                run_uuid, business_id, _map_transactions(transactions)
            )

        # Resume from execute→audit ONLY (no re-run of plan/reconcile/risk)
        publisher = EventPublisher(run_uuid, session)
        orchestrator = RunOrchestrator(session, publisher=publisher)
        state = await orchestrator.resume_after_approval(run_uuid, state)

        _running_states.pop(run_id, None)

        final_status = "failed" if state.get("error") else "completed"

        # Fire webhooks for approval.completed and run outcome
        try:
            import asyncio as _asyncio
            from src.services.webhook_dispatcher import dispatch_event as _dispatch
            _exec_results = state.get("candidate_execution_results", [])
            _succeeded = [e for e in _exec_results if e.get("execution_status") == "success"]
            _failed_exec = [e for e in _exec_results if e.get("execution_status") == "failed"]
            _total_paid = sum(float(e.get("amount", 0)) for e in _succeeded)
            webhook_payload = {
                "run_id": run_id,
                "objective": run.objective,
                "action": "approved",
                "status": final_status,
                "approved_count": approved_count,
                "rejected_count": len(state.get("rejected_candidate_ids", [])),
                "succeeded_count": len(_succeeded),
                "failed_count": len(_failed_exec),
                "total_payout_amount": _total_paid,
                "currency": "NGN",
                "date_range": {
                    "from": state.get("date_from"),
                    "to": state.get("date_to"),
                },
                "error": state.get("error"),
                "run_url": f"{Settings.FRONTEND_URL}/runs/{run_id}",
            }
            _asyncio.create_task(_dispatch(run.business_id, "approval.completed", webhook_payload))
            run_event = "run.completed" if final_status == "completed" else "run.failed"
            _asyncio.create_task(_dispatch(run.business_id, run_event, webhook_payload))
        except Exception as _wh_exc:
            logger.warning(f"Run {run_id}: webhook dispatch failed: {_wh_exc}")

        # Sync linked conversation so chat UI reflects run completion
        await _sync_conversation_after_run(
            session, run_uuid, final_status, state.get("error")
        )

        # Notify and email run creator about the outcome
        try:
            from sqlalchemy import select as _select
            if run.created_by:
                creator_row = await session.execute(
                    _select(UserModel).where(UserModel.id == run.created_by)
                )
                creator = creator_row.scalar_one_or_none()
                if creator:
                    await send_run_completed_email(
                        to=creator.email,
                        run_id=run_id,
                        objective=run.objective,
                        status=final_status,
                        approved_count=approved_count,
                        frontend_url=Settings.FRONTEND_URL,
                    )
                    notif_repo = NotificationRepository(session)
                    if final_status == "completed":
                        await notif_repo.create(
                            user_id=run.created_by,
                            business_id=run.business_id,
                            title="Run completed",
                            message=f'{approved_count} transaction{"s" if approved_count != 1 else ""} processed on run "{run.objective[:50]}".',
                            type="success",
                            resource_type="run",
                            resource_id=run_id,
                        )
                    else:
                        await notif_repo.create(
                            user_id=run.created_by,
                            business_id=run.business_id,
                            title="Run failed",
                            message=f'Run "{run.objective[:50]}" could not complete. Please review and retry.',
                            type="error",
                            resource_type="run",
                            resource_id=run_id,
                        )
                    await session.flush()
                    await session.commit()
        except Exception as _email_exc:
            logger.warning(f"Run {run_id}: failed to notify creator: {_email_exc}")

        return {
            "run_id": run_id,
            "status": final_status,
            "approved_count": approved_count,
            "current_step": state.get("current_step"),
        }
    except Exception as e:
        logger.error(f"Run {run_id} execution failed after approval: {e}")
        try:
            await session.rollback()
            recovery_status = (
                "awaiting_approval" if Settings.is_payout_simulated() else "failed"
            )
            await run_repo.update_status(run_uuid, recovery_status, str(e))
            await session.commit()

            # Mark conversation as completed even on failure so it's not stuck
            await _sync_conversation_after_run(
                session, run_uuid, "failed", str(e)
            )
        except Exception:
            logger.error(f"Run {run_id}: failed to persist error state")
        _running_states.pop(run_id, None)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/runs/{run_id}/reject")
async def reject_candidates(
    run_id: str,
    request: RejectionRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
):
    run_uuid = _parse_uuid(run_id, "run_id")
    candidate_ids = _parse_uuid_list(request.candidate_ids, "candidate_ids")
    if not candidate_ids:
        raise HTTPException(status_code=400, detail="candidate_ids must not be empty")

    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)

    # Reject doesn't trigger execution, so a plain status check suffices.
    # The approve path's CAS (awaiting_approval → executing) prevents
    # rejected candidates from being executed after approve claims the run.
    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Run is not awaiting approval (status: {run.status})",
        )

    # Business membership gate
    from sqlalchemy import select as _select_rej
    _rej_mem_q = await session.execute(
        _select_rej(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _rej_mem_q.scalars().first():
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    # Reject candidates in DB (candidate_ids already validated above)
    rejected_count = await candidate_repo.reject(candidate_ids, run_uuid)

    # Audit log: rejection action
    audit_repo = AuditRepository(session)
    await audit_repo.append(
        run_id=run_uuid,
        action="candidates_rejected",
        detail={
            "candidate_ids": [str(cid) for cid in candidate_ids],
            "rejected_count": rejected_count,
            "reason": request.reason,
        },
    )
    await session.commit()

    state = _running_states.get(run_id)
    remaining_approved = 0
    if state is not None:
        rejected_strs = [str(cid) for cid in candidate_ids]
        state["rejected_candidate_ids"] = rejected_strs
        state["approved_candidate_ids"] = [
            cid for cid in state.get("approved_candidate_ids", [])
            if cid not in rejected_strs
        ]
        remaining_approved = len(state["approved_candidate_ids"])

    logger.info(f"Run {run_id}: rejected {rejected_count} candidates")

    # Fire approval.completed webhook for rejection
    try:
        import asyncio as _asyncio
        from src.services.webhook_dispatcher import dispatch_event as _dispatch
        _total_candidates = len(state.get("scored_candidates", [])) if state is not None else None
        _asyncio.create_task(_dispatch(run.business_id, "approval.completed", {
            "run_id": run_id,
            "objective": run.objective,
            "action": "rejected",
            "rejected_count": rejected_count,
            "remaining_approved": remaining_approved,
            "total_candidate_count": _total_candidates,
            "run_url": f"{Settings.FRONTEND_URL}/runs/{run_id}",
        }))
    except Exception as _wh_exc:
        logger.warning(f"Run {run_id}: webhook dispatch failed: {_wh_exc}")

    # Notify run creator about rejection
    if run.created_by:
        try:
            notif_repo = NotificationRepository(session)
            await notif_repo.create(
                user_id=run.created_by,
                business_id=run.business_id,
                title="Candidates rejected",
                message=f'{rejected_count} candidate{"s" if rejected_count != 1 else ""} were rejected on run "{run.objective[:50]}".',
                type="warning",
                resource_type="run",
                resource_id=run_id,
            )
            await session.commit()
        except Exception as _notif_exc:
            logger.warning(f"Run {run_id}: failed to notify creator of rejection: {_notif_exc}")

    return {
        "run_id": run_id,
        "rejected_count": rejected_count,
        "remaining_approved": remaining_approved,
    }


# ------------------------------------------------------------------
# Assign Approver — PATCH /runs/{run_id}/assign-approver
# ------------------------------------------------------------------


@router.patch("/runs/{run_id}/assign-approver")
async def assign_approver(
    run_id: str,
    request: AssignApproverRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
):
    """Reassign which team member is responsible for approving a run.

    Only works while the run is in ``awaiting_approval`` status.
    The new assignee must be an active owner or approver in the business.
    Notifies the newly assigned member via in-app notification and email.
    """
    from sqlalchemy import select as _sa, update as _su

    run_uuid = _parse_uuid(run_id, "run_id")
    new_assignee_uuid = _parse_uuid(request.user_id, "user_id")

    run_repo = RunRepository(session)
    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail="The approver can only be changed while the run is awaiting approval.",
        )

    # Business membership gate — verify caller belongs to this run's business
    from sqlalchemy import select as _select_aa
    _caller_mem_q = await session.execute(
        _select_aa(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _caller_mem_q.scalars().first():
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    # Verify the new assignee is an active approver or owner in this business
    member_q = await session.execute(
        _sa(BusinessMemberModel, UserModel)
        .join(UserModel, BusinessMemberModel.user_id == UserModel.id)
        .where(
            BusinessMemberModel.user_id == new_assignee_uuid,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.role.in_(["owner", "approver"]),
            BusinessMemberModel.is_active.is_(True),
        )
    )
    member_row = member_q.first()
    if not member_row:
        raise HTTPException(
            status_code=400,
            detail="The selected user is not an active approver in this organisation.",
        )
    _, assignee_user = member_row

    # Persist the new assignment
    await session.execute(
        _su(AgentRunModel)
        .where(AgentRunModel.id == run_uuid)
        .values(assigned_to_id=new_assignee_uuid)
    )
    await session.flush()

    # In-app notification for the newly assigned approver
    try:
        candidate_count = len(run.payout_candidates) if run.payout_candidates else 0
        notif_repo = NotificationRepository(session)
        await notif_repo.create(
            user_id=new_assignee_uuid,
            business_id=run.business_id,
            title="You've been assigned to approve a run",
            message=(
                f'{candidate_count} candidate{"s" if candidate_count != 1 else ""} need your '
                f'review on run "{run.objective[:50]}".'
            ),
            type="warning",
            resource_type="run",
            resource_id=run_id,
        )
        await session.commit()
    except Exception as _notif_exc:
        logger.warning(f"Run {run_id}: failed to notify new approver: {_notif_exc}")

    # Email the newly assigned approver (best-effort, non-blocking)
    try:
        import asyncio as _asyncio_aa
        from src.services.email_service import send_run_awaiting_approval_email as _send_aa
        _asyncio_aa.create_task(
            _send_aa(
                to=assignee_user.email,
                run_id=run_id,
                objective=run.objective,
                candidate_count=len(run.payout_candidates) if run.payout_candidates else 0,
                approver_name=assignee_user.display_name or assignee_user.email,
                frontend_url=Settings.FRONTEND_URL,
            )
        )
    except Exception as _email_exc:
        logger.warning(f"Run {run_id}: failed to email new approver: {_email_exc}")

    return {
        "run_id": run_id,
        "assigned_to_id": str(new_assignee_uuid),
        "assigned_to_name": assignee_user.display_name or assignee_user.email,
        "assigned_to_email": assignee_user.email,
    }


# ------------------------------------------------------------------
# Edit Candidate — PATCH /runs/{run_id}/candidates/{candidate_id}
# ------------------------------------------------------------------


@router.patch("/runs/{run_id}/candidates/{candidate_id}")
async def update_candidate(
    run_id: str,
    candidate_id: str,
    request: UpdateCandidateRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
):
    """Edit a payout candidate's core fields before the run is approved.

    Allowed while the run is in ``awaiting_approval``.  Editing resets
    risk scoring and KYC lookup so the execution agent re-verifies the
    updated details before any money moves.

    Budget cap: if the run has a budget_cap, the edit is rejected if
    the new candidate total would exceed it.
    """
    from decimal import Decimal as _Dec

    run_uuid = _parse_uuid(run_id, "run_id")
    candidate_uuid = _parse_uuid(candidate_id, "candidate_id")

    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)

    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail="Candidates can only be edited while the run is awaiting approval.",
        )

    # Business membership gate
    from sqlalchemy import select as _select_uc
    _uc_mem_q = await session.execute(
        _select_uc(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _uc_mem_q.scalars().first():
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    # Validate amount > 0
    if request.amount is not None and request.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0.")

    # Budget cap guard: compute whether the edit would breach the cap
    if request.amount is not None and run.budget_cap is not None:
        all_candidates = await candidate_repo.get_by_run(run_uuid)
        current = next((c for c in all_candidates if c.id == candidate_uuid), None)
        if current is None:
            raise HTTPException(status_code=404, detail="Candidate not found.")
        current_total = sum(float(c.amount) for c in all_candidates)
        new_total = current_total - float(current.amount) + request.amount
        if new_total > float(run.budget_cap):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This edit would push the run total (₦{new_total:,.2f}) over the "
                    f"budget cap (₦{float(run.budget_cap):,.2f})."
                ),
            )

    # Validate institution code if being changed
    if request.institution_code:
        inst_repo = InstitutionRepository(session)
        institutions, _ = await inst_repo.get_all_active(limit=10_000)
        valid_codes = {i.institution_code for i in institutions}
        if request.institution_code not in valid_codes:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown institution code: '{request.institution_code}'.",
            )

    updated = await candidate_repo.update_fields(
        candidate_id=candidate_uuid,
        run_id=run_uuid,
        amount=_Dec(str(request.amount)) if request.amount is not None else None,
        beneficiary_name=request.beneficiary_name,
        account_number=request.account_number,
        institution_code=request.institution_code,
    )
    await session.commit()

    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="Candidate not found, or it has already been approved and cannot be edited.",
        )

    return _serialize_candidate(updated)

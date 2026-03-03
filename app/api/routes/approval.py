import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.routes.runs import _parse_uuid, _running_states
from src.agents.orchestrator import RunOrchestrator, _map_transactions
from src.agents.state import AgentState
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import (
    AuditRepository,
    BatchRepository,
    CandidateRepository,
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
    }


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
):
    run_uuid = _parse_uuid(run_id, "run_id")
    # Validate input BEFORE acquiring CAS lock to avoid wedging the run
    candidate_ids = _parse_uuid_list(request.candidate_ids, "candidate_ids")
    if not candidate_ids:
        raise HTTPException(status_code=400, detail="candidate_ids must not be empty")

    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)
    transaction_repo = TransactionRepository(session)

    # Atomically transition status to prevent race condition (concurrent approvals)
    acquired = await run_repo.transition_status(run_uuid, "awaiting_approval", "executing")
    if not acquired:
        # Either run doesn't exist, wrong status, or already claimed by another request
        run = await run_repo.get_by_id(run_uuid)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        raise HTTPException(
            status_code=409,
            detail=f"Run is not awaiting approval (status: {run.status})",
        )
    await session.commit()

    # Idempotency guard: reject if this run already has a payout batch
    batch_repo = BatchRepository(session)
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

    # Approve candidates in DB (candidate_ids already validated above)
    approved_count = await candidate_repo.approve(candidate_ids, current_user.id, run_uuid)

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
        orchestrator = RunOrchestrator(session)
        state = await orchestrator.resume_after_approval(run_uuid, state)

        _running_states.pop(run_id, None)

        final_status = "failed" if state.get("error") else "completed"
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
            await run_repo.update_status(run_uuid, "failed", str(e))
            await session.commit()
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

    return {
        "run_id": run_id,
        "rejected_count": rejected_count,
        "remaining_approved": remaining_approved,
    }

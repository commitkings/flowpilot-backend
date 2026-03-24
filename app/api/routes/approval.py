import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.runs import (
    _build_audit_entries,
    _parse_uuid,
    _running_states,
    _status_from_node,
)
from src.agents.graph import build_flowpilot_graph
from src.agents.state import AgentState
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import (
    AuditRepository,
    CandidateRepository,
    PlanStepRepository,
    RunRepository,
    TransactionRepository,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_NODE_TO_AGENT_TYPE = {
    "plan": "planner",
    "reconcile": "reconciliation",
    "risk": "risk",
    "execute": "execution",
    "audit": "audit",
}


class ApprovalRequest(BaseModel):
    candidate_ids: list[str]


class RejectionRequest(BaseModel):
    candidate_ids: list[str]
    reason: Optional[str] = None


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


def _parse_uuid_list(values: list[str], field_name: str) -> list[uuid.UUID]:
    try:
        return [uuid.UUID(value) for value in values]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


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
        "transactionReference": transaction.transaction_reference,
        "amount": float(transaction.amount),
        "currency": transaction.currency,
        "status": transaction.status,
        "channel": transaction.channel,
        "timestamp": (
            transaction.transaction_timestamp.isoformat()
            if transaction.transaction_timestamp
            else None
        ),
        "customerId": transaction.customer_id,
        "merchantId": transaction.merchant_id,
        "processorResponseCode": transaction.processor_response_code,
        "processorResponseMessage": transaction.processor_response_message,
        "settlementDate": (
            transaction.settlement_date.isoformat()
            if transaction.settlement_date
            else None
        ),
        "isAnomaly": transaction.is_anomaly,
        "anomalyReason": transaction.anomaly_reason,
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


def _map_transactions_for_persistence(transactions: list[dict]) -> list[dict]:
    mapped: list[dict] = []
    for transaction in transactions:
        reference = transaction.get("transactionReference") or transaction.get(
            "transaction_reference"
        )
        if not reference:
            continue
        mapped.append(
            {
                "transaction_reference": reference,
                "amount": transaction.get("amount", 0),
                "currency": transaction.get("currency", "NGN"),
                "status": transaction.get("status", "PENDING"),
                "channel": transaction.get("channel"),
                "transaction_timestamp": transaction.get("timestamp")
                or transaction.get("transaction_timestamp"),
                "customer_id": transaction.get("customerId")
                or transaction.get("customer_id"),
                "merchant_id": transaction.get("merchantId")
                or transaction.get("merchant_id"),
                "processor_response_code": transaction.get("processorResponseCode")
                or transaction.get("processor_response_code"),
                "processor_response_message": transaction.get("processorResponseMessage")
                or transaction.get("processor_response_message"),
                "settlement_date": transaction.get("settlementDate")
                or transaction.get("settlement_date"),
                "is_anomaly": transaction.get("isAnomaly", False)
                or transaction.get("is_anomaly", False),
                "anomaly_reason": transaction.get("anomalyReason")
                or transaction.get("anomaly_reason"),
            }
        )
    return mapped


async def _reconstruct_state_from_db(
    session: AsyncSession, run_id: uuid.UUID
) -> Optional[AgentState]:
    run_repo = RunRepository(session)
    run = await run_repo.get_by_id(run_id)
    if run is None:
        return None

    plan_steps = [_serialize_plan_step(step) for step in run.plan_steps]
    transactions = [_serialize_transaction(txn) for txn in run.transactions]
    scored_candidates = [
        _serialize_scored_candidate(candidate) for candidate in run.payout_candidates
    ]
    approved_candidate_ids = [
        str(candidate.id)
        for candidate in run.payout_candidates
        if candidate.approval_status == "approved"
    ]
    rejected_candidate_ids = [
        str(candidate.id)
        for candidate in run.payout_candidates
        if candidate.approval_status == "rejected"
    ]
    unresolved_references = [
        transaction["transactionReference"]
        for transaction in transactions
        if transaction.get("status") == "PENDING"
        and transaction.get("transactionReference")
    ]

    return {
        "run_id": str(run.id),
        "objective": run.objective,
        "constraints": run.constraints,
        "risk_tolerance": float(run.risk_tolerance),
        "budget_cap": float(run.budget_cap) if run.budget_cap is not None else None,
        "merchant_id": run.merchant_id,
        "plan_steps": plan_steps,
        "transactions": transactions,
        "reconciled_ledger": _build_reconciled_ledger(transactions),
        "unresolved_references": unresolved_references,
        "scored_candidates": scored_candidates,
        "forecast": None,
        "lookup_results": [],
        "payout_results": [],
        "payout_status_results": [],
        "approved_candidate_ids": approved_candidate_ids,
        "rejected_candidate_ids": rejected_candidate_ids,
        "audit_report": None,
        "current_step": (
            "awaiting_approval" if run.status == "awaiting_approval" else run.status
        ),
        "error": run.error_message,
        "audit_entries": [],
    }


@router.get("/runs/{run_id}/candidates")
async def get_candidates(
    run_id: str,
    approval_status: Optional[str] = None,
    session: AsyncSession = Depends(get_db_session),
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
        "candidates": [_serialize_candidate(candidate) for candidate in candidates],
        "status": run.status,
    }


@router.post("/runs/{run_id}/approve")
async def approve_candidates(
    run_id: str,
    request: ApprovalRequest,
    session: AsyncSession = Depends(get_db_session),
):
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)
    audit_repo = AuditRepository(session)
    plan_step_repo = PlanStepRepository(session)
    transaction_repo = TransactionRepository(session)

    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=400,
            detail=f"Run is not awaiting approval (status: {run.status})",
        )

    state = _running_states.get(run_id)
    if state is None:
        state = await _reconstruct_state_from_db(session, run_uuid)
        if state is None:
            raise HTTPException(status_code=404, detail="Run not found")
        _running_states[run_id] = state

    candidate_ids = _parse_uuid_list(request.candidate_ids, "candidate_ids")
    approved_count = await candidate_repo.approve(candidate_ids, run.operator_id, run_uuid)
    await session.commit()

    existing_approved = set(state.get("approved_candidate_ids", []))
    existing_approved.update(str(candidate_id) for candidate_id in candidate_ids)
    state["approved_candidate_ids"] = list(existing_approved)
    state["current_step"] = "approved"

    logger.info(f"Run {run_id}: approved {approved_count} candidates, resuming execution")

    try:
        # Re-persist transactions from state (safe: ON CONFLICT DO NOTHING)
        transactions = state.get("transactions", [])
        if transactions:
            await transaction_repo.create_batch(
                run_uuid, _map_transactions_for_persistence(transactions)
            )

        await run_repo.update_status(run_uuid, "executing")
        graph = build_flowpilot_graph()

        plan_steps = await plan_step_repo.get_by_run(run_uuid)
        plan_steps_by_agent: dict[str, list] = {}
        for plan_step in plan_steps:
            plan_steps_by_agent.setdefault(plan_step.agent_type, []).append(plan_step)

        async for step_output in graph.astream(state):
            node_name = next(iter(step_output.keys()), "unknown") if step_output else "unknown"
            node_agent_type = _NODE_TO_AGENT_TYPE.get(node_name)
            active_plan_step = None
            if node_agent_type:
                active_plan_step = next(
                    (
                        plan_step
                        for plan_step in plan_steps_by_agent.get(node_agent_type, [])
                        if plan_step.status in {"pending", "running"}
                    ),
                    None,
                )
                if active_plan_step is not None and active_plan_step.status == "pending":
                    await plan_step_repo.mark_started(active_plan_step.id)
                    active_plan_step.status = "running"

            node_audit_entries: list[dict] = []
            if isinstance(step_output, dict):
                for node_state in step_output.values():
                    if isinstance(node_state, dict):
                        node_audit_entries = node_state.pop("audit_entries", [])
                        state.update(node_state)
                        state.setdefault("audit_entries", [])
                        state["audit_entries"].extend(node_audit_entries)

            node_state = step_output.get(node_name) if isinstance(step_output, dict) else None
            if active_plan_step is not None:
                if state.get("error"):
                    await plan_step_repo.update_status(
                        active_plan_step.id,
                        "failed",
                        output_data=node_state if isinstance(node_state, dict) else None,
                        error_message=state.get("error"),
                    )
                    active_plan_step.status = "failed"
                else:
                    await plan_step_repo.mark_completed(
                        active_plan_step.id,
                        output_data=node_state if isinstance(node_state, dict) else None,
                    )
                    active_plan_step.status = "completed"

            if node_audit_entries:
                await audit_repo.append_batch(
                    _build_audit_entries(run_uuid, node_audit_entries)
                )

            await run_repo.update_status(
                run_uuid,
                _status_from_node(node_name, state),
                state.get("error"),
            )
            await session.commit()

        final_status = "failed" if state.get("error") else "completed"
        await run_repo.update_status(run_uuid, final_status, state.get("error"))

        if final_status == "completed":
            await run_repo.mark_completed(run_uuid)
        _running_states.pop(run_id, None)
        await session.commit()

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
):
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)

    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=400,
            detail=f"Run is not awaiting approval (status: {run.status})",
        )

    candidate_ids = _parse_uuid_list(request.candidate_ids, "candidate_ids")
    rejected_count = await candidate_repo.reject(candidate_ids, run_uuid)

    state = _running_states.get(run_id)
    remaining_approved = 0
    if state is not None:
        rejected_candidate_ids = [str(candidate_id) for candidate_id in candidate_ids]
        state["rejected_candidate_ids"] = rejected_candidate_ids
        approved_candidate_ids = state.get("approved_candidate_ids", [])
        state["approved_candidate_ids"] = [
            candidate_id
            for candidate_id in approved_candidate_ids
            if candidate_id not in rejected_candidate_ids
        ]
        remaining_approved = len(state["approved_candidate_ids"])

    logger.info(f"Run {run_id}: rejected {rejected_count} candidates")

    return {
        "run_id": run_id,
        "rejected_count": rejected_count,
        "remaining_approved": remaining_approved,
    }

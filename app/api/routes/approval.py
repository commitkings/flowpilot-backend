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
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import (
    AuditRepository,
    CandidateRepository,
    RunRepository,
)

logger = logging.getLogger(__name__)
router = APIRouter()


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
        raise HTTPException(status_code=409, detail="Run state unavailable for resume")

    candidate_ids = _parse_uuid_list(request.candidate_ids, "candidate_ids")
    approved_count = await candidate_repo.approve(candidate_ids, run.operator_id, run_uuid)
    await session.commit()

    state["approved_candidate_ids"] = [str(candidate_id) for candidate_id in candidate_ids]
    state["current_step"] = "approved"

    logger.info(f"Run {run_id}: approved {approved_count} candidates, resuming execution")

    try:
        await run_repo.update_status(run_uuid, "executing")
        graph = build_flowpilot_graph()
        audit_start_index = len(state.get("audit_entries", []))

        async for step_output in graph.astream(state):
            node_name = next(iter(step_output.keys()), "unknown") if step_output else "unknown"

            if isinstance(step_output, dict):
                for node_state in step_output.values():
                    if isinstance(node_state, dict):
                        state.update(node_state)

            await run_repo.update_status(
                run_uuid,
                _status_from_node(node_name, state),
                state.get("error"),
            )
            await session.commit()

        new_entries = state.get("audit_entries", [])[audit_start_index:]
        if new_entries:
            await audit_repo.append_batch(_build_audit_entries(run_uuid, new_entries))

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

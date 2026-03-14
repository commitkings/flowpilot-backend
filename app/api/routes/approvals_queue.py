"""Global approvals queue — cross-run listing of payout candidates (Gap 2)."""

import logging
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import CandidateRepository, RunRepository

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _serialize_candidate_with_run(candidate, run=None) -> dict:
    data = {
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
    if run is not None:
        data["run_objective"] = run.objective
        data["run_status"] = run.status
    return data


@router.get("/approvals")
async def list_approvals(
    approval_status: Optional[str] = Query(None),
    risk_decision: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    run_uuid = _parse_uuid(run_id, "run_id") if run_id else None
    candidate_repo = CandidateRepository(session)
    run_repo = RunRepository(session)

    candidates, total = await candidate_repo.list_all(
        run_id=run_uuid,
        approval_status=approval_status,
        risk_decision=risk_decision,
        search=search,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )

    # Batch-fetch associated runs for context
    run_ids = {c.run_id for c in candidates}
    runs_map = {}
    for rid in run_ids:
        run = await run_repo.get_by_id(rid)
        if run:
            runs_map[rid] = run

    return {
        "approvals": [
            _serialize_candidate_with_run(c, runs_map.get(c.run_id))
            for c in candidates
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }

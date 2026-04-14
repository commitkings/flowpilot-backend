"""
Public API routes — authenticated with API keys.

Base path: /public/v1
All list endpoints return a standard pagination envelope:
    { data: [...], total: int, limit: int, offset: int, has_more: bool }

Required scope per endpoint group:
    runs:read        — GET /runs, GET /runs/{run_id}
    runs:write       — POST /runs  (trigger a new run via API)
    transactions:read — GET /transactions
    audit:read       — GET /audit
    approvals:write  — POST /runs/{run_id}/approve, POST /runs/{run_id}/reject
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.api_key_auth import ApiKeyContext, get_api_key_context, require_scope
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    AgentRunModel,
    AuditLogModel,
    BusinessModel,
    PayoutCandidateModel,
    ReconciledTransactionModel,
)


async def _require_kyc_verified(business_id: uuid.UUID, session: AsyncSession) -> None:
    """Raise 403 if the business has not completed KYC verification."""
    result = await session.execute(
        select(BusinessModel).where(BusinessModel.id == business_id)
    )
    biz = result.scalar_one_or_none()
    if biz and biz.kyc_status != "verified":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Business KYC verification is required before using this endpoint.",
        )

router = APIRouter(prefix="/public/v1", tags=["public-api"])


# --------------------------------------------------------------------------- #
# Pagination helper
# --------------------------------------------------------------------------- #

def paginate(data: list, total: int, limit: int, offset: int) -> dict:
    return {
        "data": data,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #

def _ser_run(run: AgentRunModel) -> dict:
    return {
        "id": str(run.id),
        "objective": run.objective,
        "status": run.status,
        "risk_tolerance": float(run.risk_tolerance),
        "budget_cap": float(run.budget_cap) if run.budget_cap is not None else None,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def _ser_candidate(c: PayoutCandidateModel) -> dict:
    return {
        "id": str(c.id),
        "run_id": str(c.run_id),
        "beneficiary_name": c.beneficiary_name,
        "account_number": c.account_number,
        "institution_code": c.institution_code,
        "amount": float(c.amount),
        "currency": c.currency,
        "purpose": c.purpose,
        "risk_score": float(c.risk_score) if c.risk_score is not None else None,
        "risk_decision": c.risk_decision,
        "approval_status": c.approval_status,
        "execution_status": c.execution_status,
        "executed_at": c.executed_at.isoformat() if c.executed_at else None,
    }


def _ser_transaction(t: ReconciledTransactionModel) -> dict:
    return {
        "id": str(t.id),
        "run_id": str(t.run_id),
        "reference": t.interswitch_ref,
        "amount": float(t.amount),
        "currency": t.currency or "NGN",
        "direction": t.direction or "",
        "status": t.status or "",
        "counterparty_name": t.counterparty_name or "",
        "counterparty_bank": t.counterparty_bank or "",
        "narration": t.narration or "",
        "has_anomaly": bool(t.has_anomaly),
        "date": t.transaction_timestamp.isoformat() if t.transaction_timestamp else None,
    }


def _ser_audit(entry: AuditLogModel) -> dict:
    return {
        "id": entry.id,
        "run_id": str(entry.run_id),
        "agent_type": entry.agent_type,
        "action": entry.action,
        "detail": entry.detail,
        "created_at": entry.created_at.isoformat(),
    }


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

@router.get("/runs")
async def list_runs(
    ctx: ApiKeyContext = Depends(require_scope("runs:read")),
    session: AsyncSession = Depends(get_db_session),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """List payout runs for your organisation."""
    base = select(AgentRunModel).where(AgentRunModel.business_id == ctx.business_id)
    if status_filter:
        base = base.where(AgentRunModel.status == status_filter)

    total_result = await session.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = total_result.scalar_one()

    rows_result = await session.execute(
        base.order_by(AgentRunModel.created_at.desc()).limit(limit).offset(offset)
    )
    runs = rows_result.scalars().all()

    return paginate([_ser_run(r) for r in runs], total, limit, offset)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("runs:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Get a single run by ID."""
    result = await session.execute(
        select(AgentRunModel).where(
            AgentRunModel.id == run_id,
            AgentRunModel.business_id == ctx.business_id,
        )
    )
    run = result.scalars().first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return _ser_run(run)


@router.get("/runs/{run_id}/candidates")
async def list_candidates(
    run_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("runs:read")),
    session: AsyncSession = Depends(get_db_session),
    approval_status: Optional[str] = Query(None),
    execution_status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """List payout candidates for a run."""
    # Verify the run belongs to this business
    run_result = await session.execute(
        select(AgentRunModel).where(
            AgentRunModel.id == run_id,
            AgentRunModel.business_id == ctx.business_id,
        )
    )
    if not run_result.scalars().first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    base = select(PayoutCandidateModel).where(
        PayoutCandidateModel.run_id == run_id,
        PayoutCandidateModel.business_id == ctx.business_id,
    )
    if approval_status:
        base = base.where(PayoutCandidateModel.approval_status == approval_status)
    if execution_status:
        base = base.where(PayoutCandidateModel.execution_status == execution_status)

    total_result = await session.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = total_result.scalar_one()

    rows_result = await session.execute(
        base.order_by(PayoutCandidateModel.created_at.asc()).limit(limit).offset(offset)
    )
    candidates = rows_result.scalars().all()

    return paginate([_ser_candidate(c) for c in candidates], total, limit, offset)


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #

class ApprovalAction(BaseModel):
    notes: Optional[str] = Field(None, max_length=500)


@router.post("/runs/{run_id}/approve", status_code=status.HTTP_200_OK)
async def approve_run(
    run_id: uuid.UUID,
    body: ApprovalAction = ApprovalAction(),
    ctx: ApiKeyContext = Depends(require_scope("approvals:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Approve a run that is awaiting approval."""
    await _require_kyc_verified(ctx.business_id, session)
    result = await session.execute(
        select(AgentRunModel).where(
            AgentRunModel.id == run_id,
            AgentRunModel.business_id == ctx.business_id,
        )
    )
    run = result.scalars().first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is not awaiting approval (current status: {run.status})",
        )

    run.status = "executing"
    run.approved_at = datetime.now(timezone.utc)
    await session.commit()

    return {"run_id": str(run_id), "status": run.status, "approved_at": run.approved_at.isoformat()}


@router.post("/runs/{run_id}/reject", status_code=status.HTTP_200_OK)
async def reject_run(
    run_id: uuid.UUID,
    body: ApprovalAction = ApprovalAction(),
    ctx: ApiKeyContext = Depends(require_scope("approvals:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Reject a run that is awaiting approval."""
    await _require_kyc_verified(ctx.business_id, session)
    result = await session.execute(
        select(AgentRunModel).where(
            AgentRunModel.id == run_id,
            AgentRunModel.business_id == ctx.business_id,
        )
    )
    run = result.scalars().first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is not awaiting approval (current status: {run.status})",
        )

    run.status = "cancelled"
    run.cancelled_at = datetime.now(timezone.utc)
    if body.notes:
        run.error_message = body.notes
    await session.commit()

    return {"run_id": str(run_id), "status": run.status}


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #

@router.get("/transactions")
async def list_transactions(
    ctx: ApiKeyContext = Depends(require_scope("transactions:read")),
    session: AsyncSession = Depends(get_db_session),
    run_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """List reconciled transactions for your organisation."""
    base = select(ReconciledTransactionModel).where(
        ReconciledTransactionModel.business_id == ctx.business_id
    )
    if run_id is not None:
        base = base.where(ReconciledTransactionModel.run_id == run_id)

    total_result = await session.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = total_result.scalar_one()

    rows_result = await session.execute(
        base.order_by(ReconciledTransactionModel.created_at.desc()).limit(limit).offset(offset)
    )
    txns = rows_result.scalars().all()

    return paginate([_ser_transaction(t) for t in txns], total, limit, offset)


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #

@router.get("/audit")
async def list_audit(
    ctx: ApiKeyContext = Depends(require_scope("audit:read")),
    session: AsyncSession = Depends(get_db_session),
    run_id: Optional[uuid.UUID] = Query(None),
    agent_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """List audit log entries for your organisation's runs."""
    # Join with agent_run to enforce business_id isolation
    base = (
        select(AuditLogModel)
        .join(AgentRunModel, AuditLogModel.run_id == AgentRunModel.id)
        .where(AgentRunModel.business_id == ctx.business_id)
    )
    if run_id is not None:
        base = base.where(AuditLogModel.run_id == run_id)
    if agent_type:
        base = base.where(AuditLogModel.agent_type == agent_type)

    total_result = await session.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = total_result.scalar_one()

    rows_result = await session.execute(
        base.order_by(AuditLogModel.created_at.desc()).limit(limit).offset(offset)
    )
    entries = rows_result.scalars().all()

    return paginate([_ser_audit(e) for e in entries], total, limit, offset)

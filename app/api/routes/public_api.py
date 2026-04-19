"""
Public API routes — authenticated with API keys.

Base path: /public/v1
All list endpoints return a standard pagination envelope:
    { data: [...], total: int, limit: int, offset: int, has_more: bool }

Required scope per endpoint group:
    runs:read          — GET /runs, GET /runs/{run_id}, GET /runs/{run_id}/candidates
    runs:write         — POST /runs  (trigger a new run via API)
    transactions:read  — GET /transactions
    audit:read         — GET /audit
    recipients:read    — GET /recipients
    recipients:write   — POST /recipients, DELETE /recipients/{id}
    wallet:read        — GET /wallet/balance
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update as _sa_update, delete as _sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth.api_key_auth import ApiKeyContext, get_api_key_context, require_scope
from src.infrastructure.database.connection import get_db_session, get_session_factory
from src.infrastructure.database.flowpilot_models import (
    AgentRunModel,
    AiCreditTransactionModel,
    AuditLogModel,
    BusinessMemberModel,
    BusinessModel,
    PayoutCandidateModel,
    ReconciledTransactionModel,
    SavedRecipientModel,
    ScheduledRunModel,
    UserModel,
    WalletModel,
)

import logging
logger = logging.getLogger(__name__)


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
    """Serialize a run for the public API list response (IDs only for relations)."""
    return {
        "id": str(run.id),
        "objective": run.objective,
        "status": run.status,
        "risk_tolerance": float(run.risk_tolerance),
        "budget_cap": float(run.budget_cap) if run.budget_cap is not None else None,
        "platform_fee_rate": float(run.platform_fee_rate) if run.platform_fee_rate is not None else None,
        "platform_fee_amount": float(run.platform_fee_amount) if run.platform_fee_amount is not None else None,
        "error": run.error_message,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "approved_at": run.approved_at.isoformat() if run.approved_at else None,
        "created_by": str(run.created_by) if run.created_by else None,
        "approved_by": str(run.approved_by) if run.approved_by else None,
        "assigned_to_id": str(run.assigned_to_id) if run.assigned_to_id else None,
    }


def _ser_run_detail(run: AgentRunModel, created_by_user: dict | None, approved_by_user: dict | None, assigned_to_user: dict | None) -> dict:
    base = _ser_run(run)
    base["created_by_user"] = created_by_user
    base["approved_by_user"] = approved_by_user
    base["assigned_to"] = assigned_to_user
    return base


def _ser_user(user: UserModel | None) -> dict | None:
    if user is None:
        return None
    return {
        "id": str(user.id),
        "name": user.display_name or user.email,
        "email": user.email,
    }


def _ser_candidate(c: PayoutCandidateModel) -> dict:
    return {
        "id": str(c.id),
        "run_id": str(c.run_id),
        "beneficiary_name": c.beneficiary_name,
        "account_number": c.account_number,
        "beneficiary_email": c.beneficiary_email,
        "institution_code": c.institution_code,
        "amount": float(c.amount),
        "currency": c.currency,
        "purpose": c.purpose,
        "risk_score": float(c.risk_score) if c.risk_score is not None else None,
        "risk_decision": c.risk_decision,
        "lookup_status": c.lookup_status,
        "lookup_account_name": c.lookup_account_name,
        "approval_status": c.approval_status,
        "approved_by": str(c.approved_by) if c.approved_by else None,
        "approved_at": c.approved_at.isoformat() if c.approved_at else None,
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


def _ser_recipient(r: SavedRecipientModel) -> dict:
    return {
        "id": str(r.id),
        "name": r.name,
        "account_number": r.account_number,
        "institution_code": r.institution_code,
        "email": r.email,
        "notes": r.notes,
        "tags": r.tags or [],
        "payment_count": r.payment_count,
        "last_paid_at": r.last_paid_at.isoformat() if r.last_paid_at else None,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


# --------------------------------------------------------------------------- #
# Runs — list & get
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
    """Get a single run by ID, including full user details."""
    result = await session.execute(
        select(AgentRunModel).where(
            AgentRunModel.id == run_id,
            AgentRunModel.business_id == ctx.business_id,
        )
    )
    run = result.scalars().first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    async def _load_user(user_id: uuid.UUID | None) -> UserModel | None:
        if not user_id:
            return None
        r = await session.execute(select(UserModel).where(UserModel.id == user_id))
        return r.scalar_one_or_none()

    created_by_user = await _load_user(run.created_by)
    approved_by_user = await _load_user(run.approved_by)
    assigned_to_user = await _load_user(run.assigned_to_id)

    return _ser_run_detail(
        run,
        created_by_user=_ser_user(created_by_user),
        approved_by_user=_ser_user(approved_by_user),
        assigned_to_user=_ser_user(assigned_to_user),
    )


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
# Runs — create
# --------------------------------------------------------------------------- #

class CreateRunCandidateInput(BaseModel):
    beneficiary_name: str = Field(..., min_length=1, max_length=255)
    account_number: str = Field(..., min_length=5, max_length=20)
    institution_code: str = Field(..., min_length=1, max_length=10)
    amount: float = Field(..., gt=0, description="Amount in NGN")
    currency: str = Field("NGN", max_length=3)
    purpose: Optional[str] = Field(None, max_length=255)
    beneficiary_email: Optional[str] = Field(None, max_length=255)


class CreateRunRequest(BaseModel):
    objective: str = Field(..., min_length=1, max_length=2000,
                           description="Describe what this payout run should do.")
    date_from: Optional[str] = Field(None, description="ISO date string YYYY-MM-DD — start of the transaction date range.")
    date_to: Optional[str] = Field(None, description="ISO date string YYYY-MM-DD — end of the transaction date range.")
    risk_tolerance: float = Field(0.35, ge=0.0, le=1.0,
                                  description="0.0 = block all risky, 1.0 = allow all. Default 0.35.")
    budget_cap: Optional[float] = Field(None, gt=0, description="Maximum total payout amount for this run (NGN).")
    candidates: Optional[List[CreateRunCandidateInput]] = Field(
        None, description="Pre-seeded recipient list. If omitted the AI planner discovers candidates from the objective."
    )


async def _run_pipeline_bg(run_id: uuid.UUID, state: dict) -> None:
    """Background task: execute the AI pipeline with a dedicated DB session."""
    factory = get_session_factory()
    async with factory() as bg_session:
        try:
            from src.agents.orchestrator import RunOrchestrator
            from src.agents.event_publisher import EventPublisher
            publisher = EventPublisher(run_id, bg_session)
            orch = RunOrchestrator(bg_session, publisher=publisher)
            await orch.execute_run(run_id, state)
        except Exception as exc:
            logger.error("[PublicAPI] Background run %s failed: %s", run_id, exc)


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def create_run(
    body: CreateRunRequest,
    ctx: ApiKeyContext = Depends(require_scope("runs:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Create a new payout run. The AI planning and risk-scoring agents are triggered
    automatically after creation. The run starts in **pending** status and transitions
    through planning → scoring → awaiting_approval → executing → completed.

    Returns immediately — poll GET /runs/{run_id} or subscribe to webhooks to track progress.
    Requires **runs:write** scope. Business KYC must be verified.
    """
    await _require_kyc_verified(ctx.business_id, session)

    # Resolve business owner for created_by (required FK)
    owner_result = await session.execute(
        select(BusinessMemberModel)
        .where(
            BusinessMemberModel.business_id == ctx.business_id,
            BusinessMemberModel.role == "owner",
            BusinessMemberModel.is_active.is_(True),
        )
        .limit(1)
    )
    owner = owner_result.scalars().first()
    if not owner:
        raise HTTPException(status_code=422, detail="No active owner found for this business.")

    # Check AI credits
    biz_result = await session.execute(
        select(BusinessModel).where(BusinessModel.id == ctx.business_id)
    )
    biz = biz_result.scalar_one_or_none()
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found.")

    # Wallet pre-flight
    wallet_result = await session.execute(
        select(WalletModel).where(WalletModel.business_id == ctx.business_id)
    )
    wallet = wallet_result.scalar_one_or_none()
    wallet_balance = wallet.balance if wallet else Decimal("0")

    if body.candidates:
        total_amount = sum(Decimal(str(c.amount)) for c in body.candidates)
        if wallet_balance < total_amount:
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient wallet balance. Required: ₦{total_amount:,.2f}, available: ₦{wallet_balance:,.2f}.",
            )
    elif body.budget_cap is not None:
        if wallet_balance < Decimal(str(body.budget_cap)):
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient wallet balance for requested budget cap of ₦{body.budget_cap:,.2f}.",
            )

    # Atomic AI credit deduction
    credit_update = await session.execute(
        _sa_update(BusinessModel)
        .where(
            BusinessModel.id == ctx.business_id,
            BusinessModel.ai_credit_balance > 0,
        )
        .values(ai_credit_balance=BusinessModel.ai_credit_balance - 1)
        .returning(BusinessModel.id)
        .execution_options(synchronize_session=False)
    )
    if credit_update.fetchone() is None:
        raise HTTPException(
            status_code=402,
            detail="No AI processing credits remaining. Purchase a credit bundle to create new runs.",
        )

    # Parse optional date range
    date_from: date | None = None
    date_to: date | None = None
    if body.date_from:
        try:
            date_from = date.fromisoformat(body.date_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from — use YYYY-MM-DD.")
    if body.date_to:
        try:
            date_to = date.fromisoformat(body.date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to — use YYYY-MM-DD.")

    # Create run record
    from src.config.settings import Settings as _S
    run = AgentRunModel(
        business_id=ctx.business_id,
        created_by=owner.user_id,
        objective=body.objective,
        merchant_id=_S.INTERSWITCH_MERCHANT_ID,
        date_from=date_from,
        date_to=date_to,
        risk_tolerance=Decimal(str(body.risk_tolerance)),
        budget_cap=Decimal(str(body.budget_cap)) if body.budget_cap is not None else None,
        status="pending",
    )
    session.add(run)
    await session.flush()  # Get run.id before adding candidates

    # Create candidate records
    candidate_dicts: list[dict] = []
    if body.candidates:
        for c in body.candidates:
            cand = PayoutCandidateModel(
                run_id=run.id,
                business_id=ctx.business_id,
                beneficiary_name=c.beneficiary_name,
                account_number=c.account_number,
                institution_code=c.institution_code,
                beneficiary_email=c.beneficiary_email,
                amount=Decimal(str(c.amount)),
                currency=c.currency or "NGN",
                purpose=c.purpose,
                approval_status="pending",
                execution_status="not_started",
            )
            session.add(cand)
        await session.flush()

        # Re-query to get persisted IDs
        cand_result = await session.execute(
            select(PayoutCandidateModel).where(PayoutCandidateModel.run_id == run.id)
        )
        for p in cand_result.scalars().all():
            candidate_dicts.append({
                "candidate_id": str(p.id),
                "institution_code": p.institution_code,
                "beneficiary_name": p.beneficiary_name,
                "account_number": p.account_number,
                "beneficiary_email": p.beneficiary_email,
                "amount": float(p.amount),
                "currency": p.currency,
                "purpose": p.purpose,
            })

    # AI credit audit log
    credit_log = AiCreditTransactionModel(
        business_id=ctx.business_id,
        run_id=run.id,
        type="debit",
        credits=1,
        description=f"API run: {body.objective[:80]}",
    )
    session.add(credit_log)
    await session.commit()
    await session.refresh(run)

    # Build initial orchestrator state
    state: dict = {
        "run_id": str(run.id),
        "business_id": str(ctx.business_id),
        "objective": body.objective,
        "constraints": None,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "risk_tolerance": body.risk_tolerance,
        "budget_cap": body.budget_cap,
        "merchant_id": _S.INTERSWITCH_MERCHANT_ID,
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

    # Fire the AI pipeline as a background task — return immediately
    asyncio.create_task(_run_pipeline_bg(run.id, state))
    logger.info("[PublicAPI] Created run %s via API key, pipeline launched.", run.id)

    return _ser_run(run)


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


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #

class CreateRecipientBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    account_number: str = Field(..., min_length=5, max_length=32)
    institution_code: str = Field(..., min_length=1, max_length=16)
    email: Optional[str] = Field(None, max_length=256)
    notes: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


@router.get("/recipients")
async def list_recipients(
    ctx: ApiKeyContext = Depends(require_scope("recipients:read")),
    session: AsyncSession = Depends(get_db_session),
    search: Optional[str] = Query(None, max_length=256, description="Filter by name or account number"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """List saved recipients for your organisation."""
    from sqlalchemy import or_
    base = select(SavedRecipientModel).where(
        SavedRecipientModel.business_id == ctx.business_id
    )
    if search:
        term = f"%{search}%"
        base = base.where(
            or_(
                SavedRecipientModel.name.ilike(term),
                SavedRecipientModel.account_number.ilike(term),
            )
        )

    total_result = await session.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = total_result.scalar_one()

    rows_result = await session.execute(
        base.order_by(SavedRecipientModel.name.asc()).limit(limit).offset(offset)
    )
    recipients = rows_result.scalars().all()

    return paginate([_ser_recipient(r) for r in recipients], total, limit, offset)


@router.post("/recipients", status_code=status.HTTP_201_CREATED)
async def create_recipient(
    body: CreateRecipientBody,
    ctx: ApiKeyContext = Depends(require_scope("recipients:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Create a saved recipient. Useful for syncing your HR or payee system."""
    recipient = SavedRecipientModel(
        business_id=ctx.business_id,
        name=body.name,
        account_number=body.account_number,
        institution_code=body.institution_code,
        email=body.email,
        notes=body.notes,
        tags=body.tags,
    )
    session.add(recipient)
    await session.commit()
    await session.refresh(recipient)
    return _ser_recipient(recipient)


@router.delete("/recipients/{recipient_id}", status_code=status.HTTP_200_OK)
async def delete_recipient(
    recipient_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("recipients:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Delete a saved recipient by ID."""
    result = await session.execute(
        select(SavedRecipientModel).where(
            SavedRecipientModel.id == recipient_id,
            SavedRecipientModel.business_id == ctx.business_id,
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipient not found")

    await session.execute(
        _sa_delete(SavedRecipientModel).where(SavedRecipientModel.id == recipient_id)
    )
    await session.commit()
    return {"status": "deleted", "id": str(recipient_id)}


# --------------------------------------------------------------------------- #
# Wallet
# --------------------------------------------------------------------------- #

@router.get("/wallet/balance")
async def get_wallet_balance(
    ctx: ApiKeyContext = Depends(require_scope("wallet:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Return the current wallet balance for your organisation.
    Useful for checking funds before creating a run.
    """
    wallet_result = await session.execute(
        select(WalletModel).where(WalletModel.business_id == ctx.business_id)
    )
    wallet = wallet_result.scalar_one_or_none()

    biz_result = await session.execute(
        select(BusinessModel).where(BusinessModel.id == ctx.business_id)
    )
    biz = biz_result.scalar_one_or_none()

    return {
        "balance": float(wallet.balance) if wallet else 0.0,
        "currency": wallet.currency if wallet else "NGN",
        "ai_credit_balance": biz.ai_credit_balance if biz else 0,
        "updated_at": wallet.updated_at.isoformat() if wallet and hasattr(wallet, "updated_at") else None,
    }


# --------------------------------------------------------------------------- #
# Organisation profile
# --------------------------------------------------------------------------- #

@router.get("/org")
async def get_org_profile(
    ctx: ApiKeyContext = Depends(require_scope("runs:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Return the organisation profile associated with this API key."""
    biz_result = await session.execute(
        select(BusinessModel)
        .options(
            selectinload(BusinessModel.payment_policy),
            selectinload(BusinessModel.virtual_accounts),
            selectinload(BusinessModel.profile_row),
        )
        .where(BusinessModel.id == ctx.business_id)
    )
    biz = biz_result.scalar_one_or_none()
    if biz is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    return {
        "id": str(biz.id),
        "business_name": biz.business_name,
        "business_type": biz.business_type,
        "kyc_status": biz.kyc_status,
        "is_active": biz.is_active,
        "virtual_account_number": biz.virtual_account_number,
        "virtual_account_bank": biz.virtual_account_bank,
        "virtual_account_name": biz.virtual_account_name,
        "daily_payout_limit": float(biz.daily_payout_limit) if biz.daily_payout_limit else None,
        "single_payout_cap": float(biz.single_payout_cap) if biz.single_payout_cap else None,
        "risk_appetite": biz.risk_appetite,
    }


# --------------------------------------------------------------------------- #
# Scheduled runs
# --------------------------------------------------------------------------- #

@router.get("/scheduled-runs")
async def list_scheduled_runs(
    ctx: ApiKeyContext = Depends(require_scope("runs:read")),
    session: AsyncSession = Depends(get_db_session),
    is_active: Optional[bool] = Query(None),
    run_type: Optional[str] = Query(None, description="Filter by run_type: 'recurring' or 'one_time'"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """List scheduled payout runs for your organisation."""
    base = select(ScheduledRunModel).where(ScheduledRunModel.business_id == ctx.business_id)
    if is_active is not None:
        base = base.where(ScheduledRunModel.is_active == is_active)
    if run_type:
        base = base.where(ScheduledRunModel.run_type == run_type)

    total_result = await session.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = total_result.scalar_one()

    rows_result = await session.execute(
        base.order_by(ScheduledRunModel.created_at.desc()).limit(limit).offset(offset)
    )
    schedules = rows_result.scalars().all()

    def _ser_scheduled(s: ScheduledRunModel) -> dict:
        return {
            "id": str(s.id),
            "name": s.name,
            "objective": s.objective,
            "run_type": s.run_type,
            "cron_expression": s.cron_expression,
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "is_active": s.is_active,
            "created_at": s.created_at.isoformat(),
        }

    return paginate([_ser_scheduled(s) for s in schedules], total, limit, offset)

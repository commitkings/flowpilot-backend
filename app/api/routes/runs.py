import asyncio
import csv
import io
import json as json_mod
import logging
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.auth.kyc_deps import require_verified_kyc
from src.agents.orchestrator import RunOrchestrator
from src.services.email_service import send_run_awaiting_approval_email
from src.infrastructure.database.repositories.notification_repository import NotificationRepository
from src.agents.event_publisher import EventPublisher, subscribe, unsubscribe
from src.agents.state import AgentState
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import BusinessMemberModel, BusinessModel, UserModel
from src.infrastructure.database.repositories import (
    AuditRepository,
    CandidateRepository,
    InstitutionRepository,
    PlanStepRepository,
    RunRepository,
    TransactionRepository,
    RunEventRepository,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory cache of active run states (for runs awaiting approval).
# Only populated between create_run halt and approval/rejection.
_running_states: dict[str, AgentState] = {}


class CandidateInput(BaseModel):
    """A single payout candidate submitted by the operator."""
    institution_code: str = Field(..., max_length=10, description="Bank/institution code")
    beneficiary_name: str = Field(..., max_length=255)
    account_number: str = Field(..., max_length=20)
    beneficiary_email: Optional[str] = Field(None, max_length=255, description="Beneficiary email for payment notification")
    amount: float = Field(..., gt=0, description="Payout amount (must be > 0)")
    currency: str = Field("NGN", max_length=3)
    purpose: Optional[str] = Field(None, max_length=255)


class CandidateResponse(BaseModel):
    """Payout candidate with risk and approval enrichments."""
    id: str
    institution_code: str
    beneficiary_name: str
    account_number: str
    amount: float
    currency: str
    purpose: Optional[str] = None
    risk_score: Optional[float] = None
    risk_reasons: Optional[list] = None
    risk_decision: Optional[str] = None
    lookup_status: str = "pending"
    lookup_account_name: Optional[str] = None
    lookup_match_score: Optional[float] = None
    approval_status: str = "pending"
    execution_status: str = "not_started"


class CreateRunRequest(BaseModel):
    business_id: str = Field(..., description="Business UUID (multi-tenancy scope)")
    objective: str = Field(..., description="Operator objective text")
    constraints: Optional[str] = None
    date_from: Optional[date] = Field(None, description="Transaction search start date")
    date_to: Optional[date] = Field(None, description="Transaction search end date")
    risk_tolerance: float = Field(0.35, ge=0.0, le=1.0)
    budget_cap: Optional[float] = None
    merchant_id: Optional[str] = None
    candidates: Optional[list[CandidateInput]] = Field(
        None, description="Payout candidates to score and execute"
    )
    assigned_approver_id: Optional[str] = Field(
        None, description="UUID of the team member pre-assigned to approve this run"
    )


class RunResponse(BaseModel):
    run_id: str
    objective: str
    status: str
    created_at: str
    risk_tolerance: Optional[float] = None
    budget_cap: Optional[float] = None
    assigned_to_id: Optional[str] = None
    assigned_to: Optional[dict] = None  # {id, name, email}
    created_by: Optional[str] = None
    created_by_user: Optional[dict] = None  # {id, name, email}
    approved_by: Optional[str] = None
    approved_by_user: Optional[dict] = None  # {id, name, email}
    approved_at: Optional[str] = None
    plan_steps: Optional[list] = None
    candidates: Optional[list[CandidateResponse]] = None
    candidate_count: int = 0
    current_step: Optional[str] = None
    error: Optional[str] = None
    platform_fee_rate: Optional[float] = None
    platform_fee_amount: Optional[float] = None


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _normalize_institution_key(value: str) -> str:
    return "".join(char for char in value.strip().lower() if char.isalnum())


def _build_institution_alias_map(institutions) -> dict[str, str]:
    alias_map: dict[str, str] = {}

    for institution in institutions:
        aliases = [
            institution.institution_code,
            institution.institution_name,
            institution.short_name,
            institution.nip_code,
            institution.cbn_code,
        ]

        for alias in aliases:
            if not alias:
                continue

            normalized_alias = _normalize_institution_key(alias)
            if normalized_alias:
                alias_map.setdefault(normalized_alias, institution.institution_code)

    return alias_map


async def _normalize_candidate_institutions(
    rows: list[dict],
    institution_repo: InstitutionRepository,
) -> list[str]:
    if not rows:
        return []

    institutions, _ = await institution_repo.get_all_active(limit=10_000)
    alias_map = _build_institution_alias_map(institutions)
    errors: list[str] = []

    for row in rows:
        raw_value = str(row.get("institution_code", "")).strip()
        normalized_value = _normalize_institution_key(raw_value)
        resolved_code = alias_map.get(normalized_value)

        if not resolved_code:
            source_label = row.get("source_label", "Item ?")
            errors.append(
                f"{source_label}: unknown institution '{raw_value}'. "
                "Use a valid institution code or known institution alias."
            )
            continue

        row["institution_code"] = resolved_code

    return errors


def _current_step_from_status(status: str) -> str:
    """Derive a human-readable current_step from the persisted run status."""
    return {
        "pending": "created",
        "planning": "planning",
        "reconciling": "reconciling",
        "scoring": "scoring",
        "forecasting": "forecasting",
        "awaiting_approval": "awaiting_approval",
        "executing": "executing",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status, status)


def _candidates_to_response(candidates) -> list[CandidateResponse]:
    """Map PayoutCandidateModel instances to CandidateResponse dicts."""
    return [
        CandidateResponse(
            id=str(c.id),
            institution_code=c.institution_code,
            beneficiary_name=c.beneficiary_name,
            account_number=c.account_number,
            amount=float(c.amount),
            currency=c.currency,
            purpose=c.purpose,
            risk_score=float(c.risk_score) if c.risk_score is not None else None,
            risk_reasons=c.risk_reasons,
            risk_decision=c.risk_decision,
            lookup_status=c.lookup_status,
            lookup_account_name=c.lookup_account_name,
            lookup_match_score=float(c.lookup_match_score) if c.lookup_match_score is not None else None,
            approval_status=c.approval_status,
            execution_status=c.execution_status,
        )
        for c in candidates
    ]


def _resolve_approval_assignment(all_approvers: list, run, business_uuid) -> list:
    """Determine which approval-capable member(s) to notify when a run needs approval.

    Rules:
    - 0 capable members : return empty (edge case, cannot approve)
    - 1 capable member  : self-approve allowed — notify that single member
    - 2 capable members : auto-assign to the non-creator; if creator is the only one, fall back
    - 3+ capable members: honour run.assigned_to_id if set, otherwise auto-pick first
                          non-creator approver (role=approver preferred over owner)
    """
    if not all_approvers:
        return []

    if len(all_approvers) == 1:
        # Only one approval-capable person — self-approve is permitted
        return all_approvers

    # 2+ members: try to exclude the run creator
    non_creator = [(m, u) for m, u in all_approvers if m.user_id != run.created_by]

    if len(all_approvers) == 2:
        # Auto-assign to the non-creator
        return non_creator if non_creator else all_approvers[:1]

    # 3+ members: honour explicit assignment first
    if run.assigned_to_id:
        explicit = [(m, u) for m, u in all_approvers if m.user_id == run.assigned_to_id]
        if explicit:
            return explicit

    # Fall back: prefer a dedicated approver (not owner) from non-creators
    pool = non_creator if non_creator else all_approvers
    approver_role = [(m, u) for m, u in pool if m.role == "approver"]
    return approver_role[:1] if approver_role else pool[:1]


async def _notify(session, user_id, business_id, title: str, message: str,
                  type: str = "info", resource_type: str | None = None, resource_id: str | None = None):
    """Create an in-app notification — best-effort, never raises."""
    try:
        repo = NotificationRepository(session)
        await repo.create(
            user_id=user_id,
            title=title,
            message=message,
            type=type,
            business_id=business_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        await session.flush()
    except Exception as exc:
        logger.warning("Failed to create notification: %s", exc)


@router.post("/runs", response_model=RunResponse)
async def create_run(
    request: CreateRunRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
):
    operator_id = current_user.id
    business_uuid = _parse_uuid(request.business_id, "business_id")

    # Validate the user actually belongs to the requested business
    from sqlalchemy import select as _select
    membership_check = await session.execute(
        _select(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == business_uuid,
        )
    )
    if not membership_check.scalars().first():
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this organisation",
        )

    # KYC gate: business must be verified before creating runs
    biz_check = await session.execute(
        _select(BusinessModel).where(BusinessModel.id == business_uuid)
    )
    biz = biz_check.scalar_one_or_none()
    if biz and biz.kyc_status != "verified":
        kyc_msg = (
            "Your business is pending KYC verification. You'll be able to create runs once verified."
            if biz.kyc_status == "pending"
            else "Business verification (KYC) is required before creating payout runs. Please complete your KYC."
        )
        raise HTTPException(status_code=403, detail=kyc_msg)

    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)
    institution_repo = InstitutionRepository(session)

    candidate_rows: list[dict] = []
    if request.candidates:
        candidate_rows = [
            {
                "source_label": f"Candidate {index}",
                "institution_code": c.institution_code,
                "beneficiary_name": c.beneficiary_name,
                "account_number": c.account_number,
                "amount": Decimal(str(c.amount)),
                "currency": c.currency,
                "purpose": c.purpose,
                "approval_status": "pending",
                "execution_status": "not_started",
            }
            for index, c in enumerate(request.candidates, start=1)
        ]
        validation_errors = await _normalize_candidate_institutions(
            candidate_rows,
            institution_repo,
        )
        if validation_errors:
            raise HTTPException(
                status_code=400,
                detail="; ".join(validation_errors[:10]),
            )

    # ── Wallet balance pre-flight ────────────────────────────────────────────
    # Block run creation early so we don't burn AI API tokens on a payout that
    # will fail at approval time due to insufficient funds.
    #
    # • Candidates provided (CSV/direct): check exact sum of amounts.
    # • No candidates but budget_cap set: use cap as the upper-bound proxy.
    # • Neither: skip — the AI pipeline will determine amounts; approval-time
    #   check in approval.py will still catch shortfalls.
    from src.infrastructure.database.repositories.wallet_repository import (
        WalletRepository as _WalletRepo,
    )
    _wallet_repo_cf = _WalletRepo(session)
    _wallet_cf = await _wallet_repo_cf.get(business_uuid)
    _wallet_available = _wallet_cf.balance if _wallet_cf else Decimal("0")

    if candidate_rows:
        _cf_required = sum(row["amount"] for row in candidate_rows)
        if _wallet_available < _cf_required:
            raise HTTPException(status_code=402, detail="Insufficient wallet balance.")
    elif request.budget_cap is not None:
        _cf_required = Decimal(str(request.budget_cap))
        if _wallet_available < _cf_required:
            raise HTTPException(status_code=402, detail="Insufficient wallet balance.")
    # ─────────────────────────────────────────────────────────────────────────

    # ── AI processing credit — atomic deduction ──────────────────────────────
    # Single SQL UPDATE with a WHERE guard:  no TOCTOU race, balance can never
    # go negative. If 0 rows are updated the balance was already 0 (or the biz
    # row doesn't exist) — block immediately before any run is created.
    from sqlalchemy import update as _sa_update
    _credit_result = await session.execute(
        _sa_update(BusinessModel)
        .where(
            BusinessModel.id == business_uuid,
            BusinessModel.ai_credit_balance > 0,
        )
        .values(ai_credit_balance=BusinessModel.ai_credit_balance - 1)
        .returning(BusinessModel.id)
        .execution_options(synchronize_session=False)
    )
    if _credit_result.fetchone() is None:
        raise HTTPException(
            status_code=402,
            detail=(
                "No AI processing credits remaining. "
                "Purchase a credit bundle to create new payouts."
            ),
        )
    # Credit deduction is now live in the current DB transaction.
    # run_repo.create() below (flush-only) joins the same transaction so both
    # are committed atomically at the first session.commit() call.
    # ─────────────────────────────────────────────────────────────────────────

    run = await run_repo.create(
        business_id=business_uuid,
        created_by=operator_id,
        objective=request.objective,
        merchant_id=request.merchant_id or Settings.INTERSWITCH_MERCHANT_ID,
        constraints=request.constraints,
        date_from=request.date_from,
        date_to=request.date_to,
        risk_tolerance=Decimal(str(request.risk_tolerance)),
        budget_cap=(
            Decimal(str(request.budget_cap))
            if request.budget_cap is not None
            else None
        ),
    )
    # Store manually pre-assigned approver if provided (for 3+ member teams)
    if request.assigned_approver_id:
        try:
            assigned_uuid = uuid.UUID(request.assigned_approver_id)
            # Self-assignment guard: block when >1 approval-capable members exist
            if assigned_uuid == current_user.id:
                from sqlalchemy import select as _sa_sel, func as _sa_func
                _capable_result = await session.execute(
                    _sa_sel(_sa_func.count())
                    .select_from(BusinessMemberModel)
                    .where(
                        BusinessMemberModel.business_id == business_uuid,
                        BusinessMemberModel.is_active.is_(True),
                        BusinessMemberModel.role.in_(["owner", "approver"]),
                    )
                )
                capable_count = _capable_result.scalar_one()
                if capable_count > 1:
                    raise HTTPException(
                        status_code=403,
                        detail="You cannot assign yourself as the approver when other team members with approval access are available. This restriction prevents a single person from both creating and approving a payout.",
                    )
            run.assigned_to_id = assigned_uuid
        except HTTPException:
            raise
        except ValueError:
            pass  # Invalid UUID — ignore silently, auto-assign will take over

    await session.commit()
    await session.refresh(run)

    run_id = str(run.id)

    # Persist raw candidates to DB before pipeline starts
    candidate_dicts: list[dict] = []
    if candidate_rows:
        persisted = await candidate_repo.create_batch(
            run.id,
            [
                {
                    key: value
                    for key, value in row.items()
                    if key != "source_label"
                }
                for row in candidate_rows
            ],
            business_id=business_uuid,
        )
        await session.commit()
        # Build dicts for RiskAgent (matches its expected input format)
        candidate_dicts = [
            {
                "candidate_id": str(p.id),
                "institution_code": p.institution_code,
                "beneficiary_name": p.beneficiary_name,
                "account_number": p.account_number,
                "beneficiary_email": p.beneficiary_email,
                "amount": float(p.amount),
                "currency": p.currency,
                "purpose": p.purpose,
            }
            for p in persisted
        ]
        logger.info(f"Run {run_id}: ingested {len(candidate_dicts)} candidates")

    state: AgentState = {
        "run_id": run_id,
        "business_id": str(business_uuid),
        "objective": request.objective,
        "constraints": request.constraints,
        "date_from": request.date_from.isoformat() if request.date_from else None,
        "date_to": request.date_to.isoformat() if request.date_to else None,
        "risk_tolerance": request.risk_tolerance,
        "budget_cap": request.budget_cap,
        "merchant_id": request.merchant_id or Settings.INTERSWITCH_MERCHANT_ID,
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

    # ── AI credit log entry (audit trail only — deduction was already committed) ─
    from src.infrastructure.database.flowpilot_models import AiCreditTransactionModel as _CreditTxModel
    _credit_log = _CreditTxModel(
        business_id=business_uuid,
        run_id=run.id,
        type="debit",
        credits=1,
        description=f"AI analysis: {request.objective[:80]}",
    )
    session.add(_credit_log)
    # ─────────────────────────────────────────────────────────────────────────

    # Notify creator that the run has started
    await _notify(session, current_user.id, business_uuid,
                  title="Run started",
                  message=f'Your run "{request.objective[:60]}" is now processing.',
                  type="info", resource_type="run", resource_id=run_id)
    await session.commit()

    logger.info(f"Created run {run_id}: {request.objective[:80]}")

    try:
        publisher = EventPublisher(run.id, session)
        orchestrator = RunOrchestrator(session, publisher=publisher)
        state = await orchestrator.execute_run(run.id, state)

        # Derive final status from DB semantics, not agent current_step
        if state.get("current_step") == "awaiting_approval":
            final_status = "awaiting_approval"
            _running_states[run_id] = state

            # Notify all approvers and owners in the business
            # Fire approval.requested webhook
            try:
                import asyncio as _asyncio
                from src.services.webhook_dispatcher import dispatch_event as _dispatch

                _candidates = state.get("scored_candidates", [])
                _total_amount = sum(float(c.get("amount", 0)) for c in _candidates)
                _risk_breakdown = {"allow": 0, "review": 0, "block": 0}
                _flagged: list[dict] = []
                for _c in _candidates:
                    _decision = _c.get("risk_decision", "allow")
                    _risk_breakdown[_decision] = _risk_breakdown.get(_decision, 0) + 1
                    if _decision in ("review", "block"):
                        _flagged.append({
                            "candidate_id": _c.get("candidate_id"),
                            "beneficiary_name": _c.get("beneficiary_name"),
                            "account_number": _c.get("account_number"),
                            "institution_code": _c.get("institution_code"),
                            "amount": float(_c.get("amount", 0)),
                            "risk_score": float(_c.get("risk_score", 0)),
                            "risk_decision": _decision,
                            "risk_reasons": _c.get("risk_reasons", []),
                        })

                _asyncio.create_task(_dispatch(business_uuid, "approval.requested", {
                    "run_id": run_id,
                    "objective": request.objective,
                    "candidate_count": len(_candidates),
                    "total_payout_amount": _total_amount,
                    "currency": "NGN",
                    "date_range": {
                        "from": state.get("date_from"),
                        "to": state.get("date_to"),
                    },
                    "risk_breakdown": _risk_breakdown,
                    "flagged_candidates": _flagged,
                    "approval_url": f"{Settings.FRONTEND_URL}/runs/{run_id}",
                }))
            except Exception as _wh_exc:
                logger.warning(f"Run {run_id}: webhook dispatch failed: {_wh_exc}")

            try:
                from sqlalchemy import select as _select2
                # Fetch all approval-capable members for this business
                all_approvers = (await session.execute(
                    _select2(BusinessMemberModel, UserModel)
                    .join(UserModel, BusinessMemberModel.user_id == UserModel.id)
                    .where(
                        BusinessMemberModel.business_id == business_uuid,
                        BusinessMemberModel.role.in_(["owner", "approver"]),
                        BusinessMemberModel.is_active == True,
                    )
                )).all()

                candidate_count = len(state.get("scored_candidates", []))

                # Smart assignment: determine who gets notified
                assigned_members = _resolve_approval_assignment(
                    all_approvers, run, business_uuid
                )

                for member, approver_user in assigned_members:
                    from src.services.email_service import check_notification_pref as _cnp
                    if _cnp(approver_user, "payout_updates"):
                        await send_run_awaiting_approval_email(
                            to=approver_user.email,
                            run_id=run_id,
                            objective=request.objective,
                            candidate_count=candidate_count,
                            approver_name=approver_user.display_name or approver_user.email,
                            frontend_url=Settings.FRONTEND_URL,
                        )
                    await _notify(session, approver_user.id, business_uuid,
                                  title="Approval needed",
                                  message=f'{candidate_count} candidate{"s" if candidate_count != 1 else ""} need your review on run "{request.objective[:50]}".',
                                  type="warning", resource_type="run", resource_id=run_id)
                await session.commit()
            except Exception as _email_exc:
                logger.warning(f"Run {run_id}: failed to notify approvers: {_email_exc}")

        elif state.get("error"):
            final_status = "failed"
            _running_states.pop(run_id, None)
            await _notify(session, current_user.id, business_uuid,
                          title="Run failed",
                          message=f'Your run "{request.objective[:50]}" encountered an error.',
                          type="error", resource_type="run", resource_id=run_id)
            await session.commit()
        else:
            final_status = "completed"
            _running_states.pop(run_id, None)
            await _notify(session, current_user.id, business_uuid,
                          title="Run completed",
                          message=f'Your run "{request.objective[:50]}" completed successfully.',
                          type="success", resource_type="run", resource_id=run_id)
            await session.commit()

        # Load candidates from DB for response (may now have risk scores)
        db_candidates = await candidate_repo.get_by_run(run.id)
        candidate_responses = _candidates_to_response(db_candidates)

        return RunResponse(
            run_id=run_id,
            objective=run.objective,
            status=final_status,
            created_at=run.created_at.isoformat(),
            risk_tolerance=float(run.risk_tolerance),
            budget_cap=float(run.budget_cap) if run.budget_cap is not None else None,
            assigned_to_id=str(run.assigned_to_id) if run.assigned_to_id else None,
            plan_steps=state.get("plan_steps"),
            candidates=candidate_responses or None,
            candidate_count=len(candidate_responses),
            current_step=state.get("current_step"),
            error=state.get("error"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Run {run_id} failed: {e}")
        try:
            await session.rollback()
            await run_repo.update_status(run.id, "failed", str(e))
            await session.commit()
        except Exception:
            logger.error(f"Run {run_id}: failed to persist error state")
        _running_states.pop(run_id, None)
        raise HTTPException(status_code=500, detail=f"Run failed: {str(e)}")


@router.get("/runs")
async def list_runs(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    from sqlalchemy import select as _select_biz
    membership_result = await session.execute(
        _select_biz(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    membership = membership_result.scalars().first()
    if not membership:
        return {"runs": [], "total": 0, "limit": limit, "offset": offset}

    run_repo = RunRepository(session)
    runs, total = await run_repo.list_by_business(
        membership.business_id,
        status=status,
        search=search,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )
    return {
        "runs": [
            {
                "run_id": str(run.id),
                "objective": run.objective,
                "status": run.status,
                "created_at": run.created_at.isoformat(),
                "current_step": _current_step_from_status(run.status),
            }
            for run in runs
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)
    run = await run_repo.get_by_id(run_uuid)

    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Business membership gate — ensure caller belongs to this run's business
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

    db_plan_steps = [
        {
            "agent_type": s.agent_type,
            "order": s.step_order,
            "description": s.description,
            "status": s.status,
        }
        for s in (run.run_steps or [])
    ]

    # Use DB plan steps when available because they carry persisted step status.
    # Fall back to in-memory planner output only if the DB rows do not exist yet.
    state = _running_states.get(run_id)
    plan_steps = db_plan_steps or (state.get("plan_steps") if state is not None else None)
    current_step = (
        state.get("current_step")
        if state is not None
        else _current_step_from_status(run.status)
    )

    db_candidates = await candidate_repo.get_by_run(run_uuid)
    candidate_responses = _candidates_to_response(db_candidates)

    # Load assigned approver user info if set
    from sqlalchemy import select as _sat
    assigned_to: dict | None = None
    if run.assigned_to_id:
        at_result = await session.execute(
            _sat(UserModel).where(UserModel.id == run.assigned_to_id)
        )
        at_user = at_result.scalar_one_or_none()
        if at_user:
            assigned_to = {
                "id": str(at_user.id),
                "name": at_user.display_name or at_user.email,
                "email": at_user.email,
            }

    # Load creator user info
    created_by_user: dict | None = None
    if run.created_by:
        cb_result = await session.execute(
            _sat(UserModel).where(UserModel.id == run.created_by)
        )
        cb_user = cb_result.scalar_one_or_none()
        if cb_user:
            created_by_user = {
                "id": str(cb_user.id),
                "name": cb_user.display_name or cb_user.email,
                "email": cb_user.email,
            }

    # Load approver user info
    approved_by_user: dict | None = None
    if run.approved_by:
        ab_result = await session.execute(
            _sat(UserModel).where(UserModel.id == run.approved_by)
        )
        ab_user = ab_result.scalar_one_or_none()
        if ab_user:
            approved_by_user = {
                "id": str(ab_user.id),
                "name": ab_user.display_name or ab_user.email,
                "email": ab_user.email,
            }

    return RunResponse(
        run_id=run_id,
        objective=run.objective,
        status=run.status,
        created_at=run.created_at.isoformat(),
        risk_tolerance=float(run.risk_tolerance),
        budget_cap=float(run.budget_cap) if run.budget_cap is not None else None,
        assigned_to_id=str(run.assigned_to_id) if run.assigned_to_id else None,
        assigned_to=assigned_to,
        created_by=str(run.created_by) if run.created_by else None,
        created_by_user=created_by_user,
        approved_by=str(run.approved_by) if run.approved_by else None,
        approved_by_user=approved_by_user,
        approved_at=run.approved_at.isoformat() if run.approved_at else None,
        plan_steps=plan_steps or None,
        candidates=candidate_responses or None,
        candidate_count=len(candidate_responses),
        current_step=current_step,
        error=run.error_message,
        platform_fee_rate=float(run.platform_fee_rate) if run.platform_fee_rate is not None else None,
        platform_fee_amount=float(run.platform_fee_amount) if run.platform_fee_amount is not None else None,
    )


@router.get("/runs/{run_id}/status")
async def get_run_status(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    transaction_repo = TransactionRepository(session)
    candidate_repo = CandidateRepository(session)
    audit_repo = AuditRepository(session)

    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    from sqlalchemy import select as _select_mem2
    _mem_q2 = await session.execute(
        _select_mem2(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _mem_q2.scalars().first():
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    transactions_count = await transaction_repo.count_by_run(run_uuid)
    candidates_count = await candidate_repo.count_by_run(run_uuid)
    has_audit_report = (await audit_repo.count_by_run(run_uuid)) > 0

    return {
        "run_id": run_id,
        "status": run.status,
        "current_step": _current_step_from_status(run.status),
        "error": run.error_message,
        "transactions_count": transactions_count,
        "candidates_count": candidates_count,
        "has_audit_report": has_audit_report,
    }


# Required CSV columns — institution_code OR bank_name is accepted (both resolve to code)
_CSV_REQUIRED_COLS = {"beneficiary_name", "account_number", "amount"}
_CSV_INSTITUTION_ALTERNATIVES = {"institution_code", "bank_name"}
_CSV_OPTIONAL_COLS = {"currency", "purpose"}


@router.post("/runs/{run_id}/candidates/upload")
async def upload_candidates_csv(
    run_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """Upload payout candidates from a CSV file to an existing run.

    CSV must have headers: institution_code, beneficiary_name, account_number, amount
    Optional columns: currency, purpose
    """
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)
    institution_repo = InstitutionRepository(session)

    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    from sqlalchemy import select as _select_mem3
    _mem_q3 = await session.execute(
        _select_mem3(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _mem_q3.scalars().first():
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    if run.status not in ("pending", "awaiting_approval"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot add candidates to run in status '{run.status}'",
        )

    # Read and decode CSV
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # Handle BOM from Excel exports
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded CSV")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(status_code=400, detail="CSV file is empty or has no headers")

    headers = {h.strip().lower() for h in reader.fieldnames}
    missing = _CSV_REQUIRED_COLS - headers
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV missing required columns: {', '.join(sorted(missing))}",
        )
    if not (headers & _CSV_INSTITUTION_ALTERNATIVES):
        raise HTTPException(
            status_code=400,
            detail="CSV must include either an 'institution_code' column (bank code) or a 'bank_name' column (e.g. 'Access Bank').",
        )
    # Parse rows
    rows: list[dict] = []
    errors: list[str] = []
    for i, row in enumerate(reader, start=2):  # row 1 is headers
        row = {k.strip().lower(): v.strip() for k, v in row.items() if k is not None and v}
        try:
            amount = Decimal(row["amount"])
            if amount <= 0:
                raise ValueError("amount must be > 0")
        except (KeyError, InvalidOperation, ValueError) as e:
            errors.append(f"Row {i}: invalid amount — {e}")
            continue
        institution_value = row.get("institution_code") or row.get("bank_name", "")
        if not institution_value or not row.get("account_number"):
            errors.append(f"Row {i}: missing institution/bank or account_number")
            continue

        rows.append({
            "source_label": f"Row {i}",
            "institution_code": institution_value,  # alias map resolves name→code later
            "beneficiary_name": row.get("beneficiary_name", ""),
            "account_number": row["account_number"],
            "amount": amount,
            "currency": row.get("currency", "NGN"),
            "purpose": row.get("purpose"),
            "approval_status": "pending",
            "execution_status": "not_started",
        })

    if not rows:
        raise HTTPException(
            status_code=400,
            detail=f"No valid candidates in CSV. Errors: {'; '.join(errors[:10])}",
        )

    validation_errors = await _normalize_candidate_institutions(rows, institution_repo)
    if validation_errors:
        raise HTTPException(
            status_code=400,
            detail="; ".join(validation_errors[:10]),
        )

    persisted = await candidate_repo.create_batch(
        run_uuid,
        [
            {key: value for key, value in row.items() if key != "source_label"}
            for row in rows
        ],
        business_id=run.business_id,
    )
    await session.commit()

    return {
        "run_id": run_id,
        "candidates_added": len(persisted),
        "parse_errors": errors[:10] if errors else None,
        "total_rows_parsed": len(rows) + len(errors),
    }


# --------------------------------------------------------------------------- #
# Run Steps — Agent transparency & detailed step information
# --------------------------------------------------------------------------- #

class StepSummaryResponse(BaseModel):
    """Summary of a single pipeline step."""
    id: str
    agent_type: str
    step_order: int
    description: Optional[str] = None
    status: str
    progress_pct: Optional[int] = None
    duration_ms: Optional[int] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    output_summary: Optional[dict] = None
    error_message: Optional[str] = None


class StepDetailResponse(StepSummaryResponse):
    """Full detail for a single step, including input/output and audit entries."""
    input_data: Optional[dict] = None
    output_data: Optional[dict] = None
    audit_entries: list[dict] = []


def _summarize_output(output_data: dict | None, agent_type: str) -> dict | None:
    """Extract a concise summary from step output_data for the timeline view."""
    if not output_data:
        return None
    summary: dict = {}
    if agent_type == "planner" and "plan_steps" in output_data:
        steps = output_data["plan_steps"]
        summary["step_count"] = len(steps) if isinstance(steps, list) else 0
        summary["preview"] = [s.get("description", s.get("name", "?"))[:60] for s in (steps[:3] if isinstance(steps, list) else [])]
    elif agent_type == "reconciliation":
        if "transactions" in output_data:
            summary["transaction_count"] = len(output_data["transactions"]) if isinstance(output_data["transactions"], list) else 0
        if "total_transactions" in output_data:
            summary["transaction_count"] = output_data["total_transactions"]
        if "reconciled_ledger" in output_data:
            ledger = output_data["reconciled_ledger"]
            summary["total_inflow"] = ledger.get("total_inflow")
            summary["total_outflow"] = ledger.get("total_outflow")
            summary["pending_count"] = ledger.get("pending_count")
            summary["failed_count"] = ledger.get("failed_count")
    elif agent_type == "risk":
        if "scored_candidates" in output_data:
            candidates = output_data["scored_candidates"]
            summary["candidates_scored"] = len(candidates) if isinstance(candidates, list) else 0
            if isinstance(candidates, list):
                decisions = {}
                for c in candidates:
                    d = c.get("risk_decision", "unknown")
                    decisions[d] = decisions.get(d, 0) + 1
                summary["decisions"] = decisions
    elif agent_type == "execution":
        if "candidate_execution_results" in output_data:
            results = output_data["candidate_execution_results"]
            summary["executed_count"] = len(results) if isinstance(results, list) else 0
        if "batch_details" in output_data:
            bd = output_data["batch_details"]
            summary["batch_ref"] = bd.get("batch_reference") if isinstance(bd, dict) else None
            if isinstance(bd, dict):
                summary["submission_status"] = bd.get("submission_status")
                summary["accepted_count"] = bd.get("accepted_count")
                summary["rejected_count"] = bd.get("rejected_count")
    elif agent_type == "audit":
        if "audit_report" in output_data and isinstance(output_data["audit_report"], dict):
            report = output_data["audit_report"]
            summary["has_executive_summary"] = "executive_summary" in report
            summary["preview"] = str(report.get("executive_summary", ""))[:120]
    return summary or None


@router.get("/runs/{run_id}/steps")
async def get_run_steps(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """Get all pipeline steps for a run with output summaries."""
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    from sqlalchemy import select as _select_mem4
    _mem_q4 = await session.execute(
        _select_mem4(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _mem_q4.scalars().first():
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    step_repo = PlanStepRepository(session)
    steps = await step_repo.get_by_run(run_uuid)

    return {
        "run_id": run_id,
        "steps": [
            StepSummaryResponse(
                id=str(s.id),
                agent_type=s.agent_type,
                step_order=s.step_order,
                description=s.description,
                status=s.status,
                progress_pct=s.progress_pct,
                duration_ms=s.duration_ms,
                started_at=s.started_at.isoformat() if s.started_at else None,
                completed_at=s.completed_at.isoformat() if s.completed_at else None,
                output_summary=_summarize_output(s.output_data, s.agent_type),
                error_message=s.error_message,
            ).model_dump()
            for s in steps
        ],
    }


@router.get("/runs/{run_id}/steps/{step_id}")
async def get_run_step_detail(
    run_id: str,
    step_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """Get full detail for a single pipeline step including audit entries."""
    run_uuid = _parse_uuid(run_id, "run_id")
    step_uuid = _parse_uuid(step_id, "step_id")

    run_repo = RunRepository(session)
    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    from sqlalchemy import select as _select_mem5
    _mem_q5 = await session.execute(
        _select_mem5(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _mem_q5.scalars().first():
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    step_repo = PlanStepRepository(session)
    steps = await step_repo.get_by_run(run_uuid)
    step = next((s for s in steps if s.id == step_uuid), None)
    if step is None:
        raise HTTPException(status_code=404, detail="Step not found")

    audit_repo = AuditRepository(session)
    all_audits = await audit_repo.get_by_run(run_uuid)
    step_audits = [
        {
            "id": str(a.id),
            "action": a.action,
            "detail": a.detail,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in all_audits
        if a.step_id == step_uuid
    ]

    return StepDetailResponse(
        id=str(step.id),
        agent_type=step.agent_type,
        step_order=step.step_order,
        description=step.description,
        status=step.status,
        progress_pct=step.progress_pct,
        duration_ms=step.duration_ms,
        started_at=step.started_at.isoformat() if step.started_at else None,
        completed_at=step.completed_at.isoformat() if step.completed_at else None,
        output_summary=_summarize_output(step.output_data, step.agent_type),
        error_message=step.error_message,
        input_data=step.input_data,
        output_data=step.output_data,
        audit_entries=step_audits,
    ).model_dump()


# =====================================================================
# SSE Streaming — GET /runs/{run_id}/events/stream
# =====================================================================

_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


# --------------------------------------------------------------------------- #
# Nudge — remind the assigned approver to take action
# --------------------------------------------------------------------------- #

@router.post("/runs/{run_id}/nudge")
async def nudge_approver(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """Send an email + in-app notification to the assigned approver, reminding
    them that a payout run is awaiting their review.

    Only the run creator or an owner may trigger a nudge.
    The run must be in 'awaiting_approval' status.
    """
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)
    run = await run_repo.get_by_id(run_uuid)

    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Membership gate
    from sqlalchemy import select as _select_nudge
    _mem_q = await session.execute(
        _select_nudge(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    caller_membership = _mem_q.scalars().first()
    if not caller_membership:
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    # Only creator or owner may nudge
    is_creator = run.created_by and run.created_by == current_user.id
    is_owner = caller_membership.role == "owner"
    if not is_creator and not is_owner:
        raise HTTPException(status_code=403, detail="Only the payout creator or an owner can send a nudge.")

    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=400,
            detail="Nudge can only be sent when the payout is awaiting approval.",
        )

    if not run.assigned_to_id:
        raise HTTPException(status_code=400, detail="This payout has no assigned approver.")

    # Load the assigned approver
    at_result = await session.execute(
        _select_nudge(UserModel).where(UserModel.id == run.assigned_to_id)
    )
    approver_user = at_result.scalar_one_or_none()
    if not approver_user:
        raise HTTPException(status_code=404, detail="Assigned approver not found.")

    # Count candidates
    db_candidates = await candidate_repo.get_by_run(run_uuid)
    candidate_count = len(db_candidates)

    # In-app notification
    await _notify(
        session,
        approver_user.id,
        run.business_id,
        title="Reminder: Payout awaiting your approval",
        message=(
            f'The payout "{run.objective[:60]}" is still waiting for your review. '
            "Please approve or reject it to proceed."
        ),
        type="warning",
        resource_type="run",
        resource_id=run_id,
    )
    await session.commit()

    # Email — fire-and-forget
    try:
        from src.services.email_service import check_notification_pref as _cnp2
        if _cnp2(approver_user, "payout_updates"):
            import asyncio as _asyncio
            _asyncio.create_task(
                send_run_awaiting_approval_email(
                    to=approver_user.email,
                    run_id=run_id,
                    objective=run.objective,
                    candidate_count=candidate_count,
                    approver_name=approver_user.display_name or approver_user.email,
                )
            )
    except Exception as exc:
        logger.warning("[Nudge] Could not send nudge email: %s", exc)

    return {"ok": True, "nudged_user": approver_user.email}


# --------------------------------------------------------------------------- #
# Rerun — edit objective/params and re-trigger pipeline from awaiting_approval
# --------------------------------------------------------------------------- #

class RerunRequest(BaseModel):
    objective: str = Field(..., min_length=1, max_length=2000)
    constraints: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    risk_tolerance: float = Field(0.35, ge=0.0, le=1.0)
    budget_cap: Optional[float] = None


@router.post("/runs/{run_id}/rerun", response_model=RunResponse)
async def rerun_payout(
    run_id: str,
    body: RerunRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """Edit and re-trigger a payout that is awaiting approval.

    Only the run creator or an owner may call this.
    The run must be in 'awaiting_approval' status.

    Steps:
      1. Validate status and permissions.
      2. Update objective and parameters on the run.
      3. Delete existing payout candidates.
      4. Reset run state to pending.
      5. Notify assigned approver that the run has been updated.
      6. Re-launch the orchestration pipeline.
    """
    from datetime import datetime as _dt, timezone as _tz
    from sqlalchemy import delete as _sa_delete

    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)
    run = await run_repo.get_by_id(run_uuid)

    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status != "awaiting_approval":
        raise HTTPException(
            status_code=400,
            detail=f"Rerun is only allowed when the payout is awaiting approval (current status: {run.status}).",
        )

    # Membership gate
    from sqlalchemy import select as _sel_rr
    _mem_rr = await session.execute(
        _sel_rr(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    caller_membership = _mem_rr.scalars().first()
    if not caller_membership:
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    is_creator = run.created_by and run.created_by == current_user.id
    is_owner = caller_membership.role == "owner"
    if not is_creator and not is_owner:
        raise HTTPException(
            status_code=403,
            detail="Only the payout creator or an owner can edit and rerun this payout.",
        )

    # Capture assigned approver before reset (for notification)
    assigned_to_id = run.assigned_to_id

    # Delete all existing payout candidates for this run
    from src.infrastructure.database.flowpilot_models import PayoutCandidateModel
    await session.execute(
        _sa_delete(PayoutCandidateModel).where(PayoutCandidateModel.run_id == run_uuid)
    )

    # Update run fields
    now = _dt.now(_tz.utc)
    run.objective = body.objective
    run.constraints = body.constraints
    run.date_from = body.date_from
    run.date_to = body.date_to
    run.risk_tolerance = Decimal(str(body.risk_tolerance))
    run.budget_cap = Decimal(str(body.budget_cap)) if body.budget_cap is not None else None

    # Reset run state
    run.status = "pending"
    run.error_message = None
    run.plan_graph = None
    run.approved_by = None
    run.approved_at = None
    run.started_at = None
    run.completed_at = None
    run.cancelled_by = None
    run.cancelled_at = None
    run.updated_at = now

    # ── AI credit deduction for rerun (same atomic guard as initial creation) ──
    # Rerun re-runs the full AI pipeline — costs 1 credit just like a new payout.
    # Use run_id=None in the log to avoid the partial-unique-index constraint
    # that prevents duplicate debit entries per run.
    from sqlalchemy import update as _sa_update_rr
    _rerun_credit_result = await session.execute(
        _sa_update_rr(BusinessModel)
        .where(
            BusinessModel.id == run.business_id,
            BusinessModel.ai_credit_balance > 0,
        )
        .values(ai_credit_balance=BusinessModel.ai_credit_balance - 1)
        .returning(BusinessModel.id)
        .execution_options(synchronize_session=False)
    )
    if _rerun_credit_result.fetchone() is None:
        raise HTTPException(
            status_code=402,
            detail=(
                "No AI processing credits remaining. "
                "Purchase a credit bundle to rerun this payout."
            ),
        )
    # ──────────────────────────────────────────────────────────────────────────

    await session.commit()
    await session.refresh(run)

    # AI credit log for the rerun (run_id=None avoids unique-index conflict)
    from src.infrastructure.database.flowpilot_models import AiCreditTransactionModel as _CreditTxModelRR
    _rerun_credit_log = _CreditTxModelRR(
        business_id=run.business_id,
        run_id=None,
        type="debit",
        credits=1,
        description=f"AI rerun: {body.objective[:80]} (run {run_id[:8]})",
    )
    session.add(_rerun_credit_log)
    await session.commit()

    # Notify assigned approver that the payout was updated
    if assigned_to_id:
        try:
            from sqlalchemy import select as _sel_at
            _at_result = await session.execute(
                _sel_at(UserModel).where(UserModel.id == assigned_to_id)
            )
            approver_user = _at_result.scalar_one_or_none()
            if approver_user:
                await _notify(
                    session,
                    approver_user.id,
                    run.business_id,
                    title="Payout updated and resubmitted",
                    message=(
                        f'The payout "{body.objective[:60]}" has been updated and resubmitted for analysis. '
                        "You will be notified again when it is ready for your review."
                    ),
                    type="info",
                    resource_type="run",
                    resource_id=run_id,
                )
                await session.commit()
        except Exception as exc:
            logger.warning("[Rerun] Could not notify approver: %s", exc)

    logger.info(f"Rerun triggered for run {run_id} by {current_user.id}")

    # Re-launch orchestration pipeline
    state: AgentState = {
        "run_id": run_id,
        "business_id": str(run.business_id),
        "objective": body.objective,
        "constraints": body.constraints,
        "date_from": body.date_from.isoformat() if body.date_from else None,
        "date_to": body.date_to.isoformat() if body.date_to else None,
        "risk_tolerance": body.risk_tolerance,
        "budget_cap": body.budget_cap,
        "merchant_id": run.merchant_id,
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

    try:
        publisher = EventPublisher(run.id, session)
        orchestrator = RunOrchestrator(session, publisher=publisher)
        state = await orchestrator.execute_run(run.id, state)

        if state.get("current_step") == "awaiting_approval":
            _running_states[run_id] = state

            # Notify assigned approver that review is needed again
            if assigned_to_id:
                try:
                    from sqlalchemy import select as _sel_at2
                    _at2_result = await session.execute(
                        _sel_at2(UserModel).where(UserModel.id == assigned_to_id)
                    )
                    approver_user2 = _at2_result.scalar_one_or_none()
                    if approver_user2:
                        db_candidates2 = await candidate_repo.get_by_run(run_uuid)
                        from src.services.email_service import check_notification_pref as _cnp3
                        if _cnp3(approver_user2, "payout_updates"):
                            await send_run_awaiting_approval_email(
                                to=approver_user2.email,
                                run_id=run_id,
                                objective=body.objective,
                                candidate_count=len(db_candidates2),
                                approver_name=approver_user2.display_name or approver_user2.email,
                            )
                except Exception as exc:
                    logger.warning("[Rerun] Could not re-notify approver: %s", exc)

    except Exception as exc:
        logger.exception(f"Rerun pipeline failed for run {run_id}: {exc}")
        run.status = "failed"
        run.error_message = str(exc)[:500]
        await session.commit()

    await session.refresh(run)
    db_candidates = await candidate_repo.get_by_run(run_uuid)
    return RunResponse(
        run_id=str(run.id),
        objective=run.objective,
        status=run.status,
        created_at=run.created_at.isoformat(),
        risk_tolerance=float(run.risk_tolerance),
        budget_cap=float(run.budget_cap) if run.budget_cap else None,
        assigned_to_id=str(run.assigned_to_id) if run.assigned_to_id else None,
        created_by=str(run.created_by) if run.created_by else None,
        error=run.error_message,
        candidate_count=len(db_candidates),
    )


@router.get("/runs/{run_id}/events/stream")
async def stream_run_events(
    run_id: str,
    last_seq: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    _user=Depends(get_current_user),
):
    """Server-Sent Events stream for real-time run observability.

    - Replays persisted events with sequence_num > last_seq
    - Subscribes to live broadcast for new events
    - Auto-closes when run reaches a terminal state
    """
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    from sqlalchemy import select as _select_mem6
    _mem_q6 = await session.execute(
        _select_mem6(BusinessMemberModel).where(
            BusinessMemberModel.user_id == _user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _mem_q6.scalars().first():
        raise HTTPException(status_code=403, detail="You do not have access to this run")

    async def _event_generator():
        event_repo = RunEventRepository(session)
        seq = last_seq

        # 1. Replay persisted events that the client missed
        past_events = await event_repo.get_events_since(run_uuid, seq)
        for evt in past_events:
            payload = {
                "seq": evt.sequence_num,
                "type": evt.event_type,
                "step_id": str(evt.step_id) if evt.step_id else None,
                "payload": evt.payload,
                "emitted_at": evt.emitted_at.isoformat() if evt.emitted_at else None,
            }
            seq = max(seq, evt.sequence_num)
            yield f"id: {seq}\nevent: {evt.event_type}\ndata: {json_mod.dumps(payload)}\n\n"

        # If already terminal, close after replay
        current_run = await run_repo.get_by_id(run_uuid)
        if current_run and current_run.status in _TERMINAL_STATUSES:
            yield f"event: done\ndata: {json_mod.dumps({'status': current_run.status})}\n\n"
            return

        # 2. Subscribe to live broadcast
        queue = subscribe(str(run_uuid))
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                seq = event.get("seq", seq + 1)
                evt_type = event.get("type", "unknown")
                payload = {
                    "seq": seq,
                    "type": evt_type,
                    "step_id": event.get("step_id"),
                    "payload": event.get("payload", {}),
                    "emitted_at": event.get("emitted_at"),
                }
                yield f"id: {seq}\nevent: {evt_type}\ndata: {json_mod.dumps(payload, default=str)}\n\n"

                if evt_type in ("run_completed", "run_failed"):
                    yield f"event: done\ndata: {json_mod.dumps({'status': evt_type.replace('run_', '')})}\n\n"
                    return
        finally:
            unsubscribe(str(run_uuid), queue)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Receipt email ─────────────────────────────────────────────────────────────

class SendReceiptEmailRequest(BaseModel):
    email: str = Field(..., description="Recipient email address")
    # Optionally scope to a single beneficiary by candidate_id
    candidate_id: Optional[str] = Field(None, description="If set, send a single-beneficiary receipt")


@router.post("/{run_id}/receipt/email")
async def send_receipt_email_endpoint(
    run_id: str,
    body: SendReceiptEmailRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Send a payment receipt email for a completed payout run.

    If candidate_id is provided, sends a single-beneficiary receipt.
    Otherwise sends the full batch receipt to the given address.
    """
    import datetime as _dt
    from sqlalchemy import select as _sel_re
    from src.services.email_service import send_receipt_email as _send_receipt

    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
    cand_repo = CandidateRepository(session)

    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status not in ("completed", "completed_with_errors"):
        raise HTTPException(status_code=400, detail="Receipts are only available for completed runs")

    # Membership check
    _mem_re = await session.execute(
        _sel_re(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.business_id == run.business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    if not _mem_re.scalars().first():
        raise HTTPException(status_code=403, detail="Access denied")

    # Org name
    _biz_re = await session.execute(
        _sel_re(BusinessModel).where(BusinessModel.id == run.business_id)
    )
    biz = _biz_re.scalar_one_or_none()
    org_name = biz.business_name if biz else "Your Organisation"

    # Approved-by name
    approved_by_name: Optional[str] = None
    if run.approved_by:
        _approver_re = await session.execute(
            _sel_re(UserModel).where(UserModel.id == run.approved_by)
        )
        _approver = _approver_re.scalar_one_or_none()
        if _approver:
            approved_by_name = _approver.display_name or _approver.email

    # Candidates
    all_candidates = await cand_repo.get_by_run(run_uuid)

    if body.candidate_id:
        target_uuid = _parse_uuid(body.candidate_id, "candidate_id")
        single = next((c for c in all_candidates if c.id == target_uuid), None)
        if single is None:
            raise HTTPException(status_code=404, detail="Candidate not found in this run")
        receipt_candidates = [single]
    else:
        receipt_candidates = all_candidates

    # Build candidate rows for the template
    def _cand_status(c) -> str:
        if c.execution_status == "success":
            return "Paid"
        if c.execution_status == "failed":
            return "Failed"
        if c.risk_decision == "block" or c.approval_status == "blocked":
            return "Held Back"
        return "Pending"

    inst_repo = InstitutionRepository(session)
    all_institutions = await inst_repo.get_all_active()
    inst_map = {i.institution_code: i.institution_name for i in all_institutions}

    candidate_rows = [
        {
            "name": c.beneficiary_name,
            "bank": inst_map.get(c.institution_code, c.institution_code),
            "account": c.account_number,
            "amount": f"{float(c.amount):,.2f}",
            "status": _cand_status(c),
        }
        for c in receipt_candidates
    ]

    # Financial summary
    successful = [c for c in all_candidates if c.execution_status == "success"]
    payout_total = float(sum(c.amount for c in successful))
    platform_fee_rate = float(run.platform_fee_rate) if run.platform_fee_rate else 0.002
    platform_fee = float(run.platform_fee_amount) if run.platform_fee_amount else round(payout_total * platform_fee_rate, 2)
    total_deducted = payout_total + platform_fee

    receipt_date = (run.approved_at or run.started_at or _dt.datetime.now(_dt.timezone.utc)).strftime(
        "%d %b %Y, %I:%M %p WAT"
    )

    ok = await _send_receipt(
        to=body.email,
        org_name=org_name,
        run_id_short=run_id[:8].upper(),
        run_status="Completed" if run.status == "completed" else "Completed (with errors)",
        receipt_date=receipt_date,
        objective=run.objective or "",
        candidates=candidate_rows,
        payout_total=payout_total,
        platform_fee=platform_fee,
        total_deducted=total_deducted,
        fee_rate_pct=platform_fee_rate * 100,
        successful_count=len(successful),
        approved_by=approved_by_name,
    )

    if not ok:
        raise HTTPException(status_code=503, detail="Failed to send email. Please try again.")

    return {"message": f"Receipt sent to {body.email}"}

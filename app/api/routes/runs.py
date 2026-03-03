import csv
import io
import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.agents.orchestrator import RunOrchestrator
from src.agents.state import AgentState
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import (
    AuditRepository,
    CandidateRepository,
    RunRepository,
    TransactionRepository,
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
    approval_status: str = "pending"
    execution_status: str = "not_started"


class CreateRunRequest(BaseModel):
    business_id: str = Field(..., description="Business UUID (multi-tenancy scope)")
    objective: str = Field(..., description="Operator objective text")
    constraints: Optional[str] = None
    risk_tolerance: float = Field(0.35, ge=0.0, le=1.0)
    budget_cap: Optional[float] = None
    merchant_id: Optional[str] = None
    candidates: Optional[list[CandidateInput]] = Field(
        None, description="Payout candidates to score and execute"
    )


class RunResponse(BaseModel):
    run_id: str
    objective: str
    status: str
    created_at: str
    plan_steps: Optional[list] = None
    candidates: Optional[list[CandidateResponse]] = None
    candidate_count: int = 0
    current_step: Optional[str] = None
    error: Optional[str] = None


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


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
            approval_status=c.approval_status,
            execution_status=c.execution_status,
        )
        for c in candidates
    ]


@router.post("/runs", response_model=RunResponse)
async def create_run(
    request: CreateRunRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    operator_id = current_user.id
    business_uuid = _parse_uuid(request.business_id, "business_id")
    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)

    run = await run_repo.create(
        business_id=business_uuid,
        created_by=operator_id,
        objective=request.objective,
        merchant_id=request.merchant_id or Settings.INTERSWITCH_MERCHANT_ID,
        constraints=request.constraints,
        risk_tolerance=Decimal(str(request.risk_tolerance)),
        budget_cap=(
            Decimal(str(request.budget_cap))
            if request.budget_cap is not None
            else None
        ),
    )
    await session.commit()
    await session.refresh(run)

    run_id = str(run.id)

    # Persist raw candidates to DB before pipeline starts
    candidate_dicts: list[dict] = []
    if request.candidates:
        raw_rows = [
            {
                "institution_code": c.institution_code,
                "beneficiary_name": c.beneficiary_name,
                "account_number": c.account_number,
                "amount": Decimal(str(c.amount)),
                "currency": c.currency,
                "purpose": c.purpose,
                "approval_status": "pending",
                "execution_status": "not_started",
            }
            for c in request.candidates
        ]
        persisted = await candidate_repo.create_batch(run.id, raw_rows, business_id=business_uuid)
        await session.commit()
        # Build dicts for RiskAgent (matches its expected input format)
        candidate_dicts = [
            {
                "candidate_id": str(p.id),
                "institution_code": p.institution_code,
                "beneficiary_name": p.beneficiary_name,
                "account_number": p.account_number,
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
    }

    logger.info(f"Created run {run_id}: {request.objective[:80]}")

    try:
        orchestrator = RunOrchestrator(session)
        state = await orchestrator.execute_run(run.id, state)

        # Derive final status from DB semantics, not agent current_step
        if state.get("current_step") == "awaiting_approval":
            final_status = "awaiting_approval"
            _running_states[run_id] = state
        elif state.get("error"):
            final_status = "failed"
            _running_states.pop(run_id, None)
        else:
            final_status = "completed"
            _running_states.pop(run_id, None)

        # Load candidates from DB for response (may now have risk scores)
        db_candidates = await candidate_repo.get_by_run(run.id)
        candidate_responses = _candidates_to_response(db_candidates)

        return RunResponse(
            run_id=run_id,
            objective=run.objective,
            status=final_status,
            created_at=run.created_at.isoformat(),
            plan_steps=state.get("plan_steps"),
            candidates=candidate_responses or None,
            candidate_count=len(candidate_responses),
            current_step=state.get("current_step"),
            error=state.get("error"),
        )
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
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    run_repo = RunRepository(session)
    runs = await run_repo.list_all()
    return [
        {
            "run_id": str(run.id),
            "objective": run.objective,
            "status": run.status,
            "created_at": run.created_at.isoformat(),
            "current_step": _current_step_from_status(run.status),
        }
        for run in runs
    ]


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

    # Prefer in-memory state for active runs, fall back to DB
    state = _running_states.get(run_id)
    if state is not None:
        plan_steps = state.get("plan_steps")
        current_step = state.get("current_step")
    else:
        plan_steps = [
            {
                "agent_type": s.agent_type,
                "order": s.step_order,
                "description": s.description,
                "status": s.status,
            }
            for s in (run.run_steps or [])
        ]
        current_step = _current_step_from_status(run.status)

    db_candidates = await candidate_repo.get_by_run(run_uuid)
    candidate_responses = _candidates_to_response(db_candidates)

    return RunResponse(
        run_id=run_id,
        objective=run.objective,
        status=run.status,
        created_at=run.created_at.isoformat(),
        plan_steps=plan_steps or None,
        candidates=candidate_responses or None,
        candidate_count=len(candidate_responses),
        current_step=current_step,
        error=run.error_message,
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


# Required CSV columns (must match CandidateInput fields)
_CSV_REQUIRED_COLS = {"institution_code", "beneficiary_name", "account_number", "amount"}
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

    run = await run_repo.get_by_id(run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
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
        if not row.get("institution_code") or not row.get("account_number"):
            errors.append(f"Row {i}: missing institution_code or account_number")
            continue

        rows.append({
            "institution_code": row["institution_code"],
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

    persisted = await candidate_repo.create_batch(run_uuid, rows, business_id=run.business_id)
    await session.commit()

    return {
        "run_id": run_id,
        "candidates_added": len(persisted),
        "parse_errors": errors[:10] if errors else None,
        "total_rows_parsed": len(rows) + len(errors),
    }

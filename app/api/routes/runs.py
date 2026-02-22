import logging
import uuid
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

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

_DEFAULT_OPERATOR_ID = "00000000-0000-0000-0000-000000000001"


class CreateRunRequest(BaseModel):
    operator_id: str = Field(_DEFAULT_OPERATOR_ID, description="Operator UUID")
    objective: str = Field(..., description="Operator objective text")
    constraints: Optional[str] = None
    risk_tolerance: float = Field(0.35, ge=0.0, le=1.0)
    budget_cap: Optional[float] = None
    merchant_id: Optional[str] = None


class RunResponse(BaseModel):
    run_id: str
    objective: str
    status: str
    created_at: str
    plan_steps: Optional[list] = None
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


@router.post("/runs", response_model=RunResponse)
async def create_run(
    request: CreateRunRequest,
    session: AsyncSession = Depends(get_db_session),
):
    operator_id = _parse_uuid(request.operator_id, "operator_id")
    run_repo = RunRepository(session)

    run = await run_repo.create(
        operator_id=operator_id,
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
    state: AgentState = {
        "run_id": run_id,
        "objective": request.objective,
        "constraints": request.constraints,
        "risk_tolerance": request.risk_tolerance,
        "budget_cap": request.budget_cap,
        "merchant_id": request.merchant_id or Settings.INTERSWITCH_MERCHANT_ID,
        "plan_steps": [],
        "transactions": [],
        "reconciled_ledger": {},
        "unresolved_references": [],
        "scored_candidates": [],
        "forecast": None,
        "lookup_results": [],
        "payout_results": [],
        "payout_status_results": [],
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

        return RunResponse(
            run_id=run_id,
            objective=run.objective,
            status=final_status,
            created_at=run.created_at.isoformat(),
            plan_steps=state.get("plan_steps"),
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
async def list_runs(session: AsyncSession = Depends(get_db_session)):
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
):
    run_uuid = _parse_uuid(run_id, "run_id")
    run_repo = RunRepository(session)
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
            for s in (run.plan_steps or [])
        ]
        current_step = _current_step_from_status(run.status)

    return RunResponse(
        run_id=run_id,
        objective=run.objective,
        status=run.status,
        created_at=run.created_at.isoformat(),
        plan_steps=plan_steps or None,
        current_step=current_step,
        error=run.error_message,
    )


@router.get("/runs/{run_id}/status")
async def get_run_status(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
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

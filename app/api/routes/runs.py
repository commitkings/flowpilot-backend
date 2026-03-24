import logging
import uuid
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.graph import build_flowpilot_graph
from src.agents.state import AgentState
from src.config.settings import Settings
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

_running_states: dict[str, AgentState] = {}
_DEFAULT_OPERATOR_ID = "00000000-0000-0000-0000-000000000001"
_NODE_TO_AGENT_TYPE = {
    "plan": "planner",
    "reconcile": "reconciliation",
    "risk": "risk",
    "execute": "execution",
    "audit": "audit",
}


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


def _status_from_node(node_name: str, state: AgentState) -> str:
    status_map = {
        "plan": "planning",
        "reconcile": "reconciling",
        "risk": "scoring",
        "approval_gate": "awaiting_approval",
        "execute": "executing",
    }
    if node_name == "audit":
        return "failed" if state.get("error") else "completed"
    return status_map.get(node_name, "planning")


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


def _build_audit_entries(run_id: uuid.UUID, entries: list[dict]) -> list[dict]:
    return [
        {
            "run_id": run_id,
            "step_id": entry.get("step_id"),
            "agent_type": entry.get("agent_type"),
            "action": entry.get("action", "unknown"),
            "detail": entry.get("detail"),
            "api_endpoint": entry.get("api_endpoint"),
            "request_hash": entry.get("request_hash"),
            "response_status": entry.get("response_status"),
            "response_time_ms": entry.get("response_time_ms"),
        }
        for entry in entries
    ]


def _map_plan_steps(steps: list[dict]) -> list[dict]:
    """Map planner agent output to PlanStepModel column names."""
    mapped_steps = [
        {
            "agent_type": s.get("agent_type", ""),
            "step_order": s.get("order", idx + 1),
            "description": s.get("description"),
            "status": "pending",
        }
        for idx, s in enumerate(steps)
    ]
    if not any(step["agent_type"] == "planner" for step in mapped_steps):
        mapped_steps.insert(
            0,
            {
                "agent_type": "planner",
                "step_order": 0,
                "description": "Generate execution plan",
                "status": "pending",
            },
        )
    return mapped_steps


def _map_transactions(txns: list[dict]) -> list[dict]:
    """Map Interswitch transaction dicts to TransactionModel column names."""
    return [
        {
            "transaction_reference": t["transactionReference"],
            "amount": t["amount"],
            "currency": t.get("currency", "NGN"),
            "status": t["status"],
            "channel": t.get("channel"),
            "transaction_timestamp": t.get("timestamp"),
            "customer_id": t.get("customerId"),
        }
        for t in txns
    ]


@router.post("/runs", response_model=RunResponse)
async def create_run(
    request: CreateRunRequest,
    session: AsyncSession = Depends(get_db_session),
):
    operator_id = _parse_uuid(request.operator_id, "operator_id")
    run_repo = RunRepository(session)
    audit_repo = AuditRepository(session)
    plan_step_repo = PlanStepRepository(session)
    transaction_repo = TransactionRepository(session)

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
    initial_state: AgentState = {
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
    _running_states[run_id] = initial_state

    logger.info(f"Created run {run_id}: {request.objective[:80]}")

    try:
        await run_repo.mark_started(run.id)
        await run_repo.update_status(run.id, "planning")

        graph = build_flowpilot_graph()
        audit_flush_index = len(initial_state.get("audit_entries", []))
        plan_step_ids_by_agent_type: dict[str, uuid.UUID] = {}
        started_plan_step_ids: set[uuid.UUID] = set()

        async for step_output in graph.astream(initial_state):
            node_name = next(iter(step_output.keys()), "unknown") if step_output else "unknown"
            logger.info(f"Run {run_id}: completed step '{node_name}'")

            if isinstance(step_output, dict):
                for node_state in step_output.values():
                    if isinstance(node_state, dict):
                        initial_state.update(node_state)

            # Persist plan_steps after the plan node completes
            if node_name == "plan" and initial_state.get("plan_steps"):
                mapped_steps = _map_plan_steps(initial_state["plan_steps"])
                persisted_steps = await plan_step_repo.create_batch(run.id, mapped_steps)
                plan_step_ids_by_agent_type = {
                    step.agent_type: step.id for step in persisted_steps
                }
                await run_repo.update_plan_graph(
                    run.id, {"steps": initial_state["plan_steps"]}
                )

            # Persist transactions after the reconcile node completes
            if node_name == "reconcile" and initial_state.get("transactions"):
                mapped_txns = _map_transactions(initial_state["transactions"])
                await transaction_repo.create_batch(run.id, mapped_txns)

            agent_type = _NODE_TO_AGENT_TYPE.get(node_name)
            step_id = (
                plan_step_ids_by_agent_type.get(agent_type) if agent_type else None
            )
            if step_id is not None:
                if step_id not in started_plan_step_ids:
                    await plan_step_repo.mark_started(step_id)
                    started_plan_step_ids.add(step_id)

                if initial_state.get("error"):
                    await plan_step_repo.update_status(
                        step_id,
                        "failed",
                        error_message=initial_state.get("error"),
                    )
                else:
                    node_output = (
                        step_output.get(node_name)
                        if isinstance(step_output, dict)
                        else None
                    )
                    await plan_step_repo.mark_completed(
                        step_id,
                        node_output if isinstance(node_output, dict) else None,
                    )

            # Flush new audit entries after each node
            all_entries = initial_state.get("audit_entries", [])
            new_entries = all_entries[audit_flush_index:]
            if new_entries:
                await audit_repo.append_batch(_build_audit_entries(run.id, new_entries))
                audit_flush_index = len(all_entries)

            await run_repo.update_status(
                run.id,
                _status_from_node(node_name, initial_state),
                initial_state.get("error"),
            )
            await session.commit()

            if initial_state.get("current_step") == "awaiting_approval":
                break

        final_status = (
            "awaiting_approval"
            if initial_state.get("current_step") == "awaiting_approval"
            else ("failed" if initial_state.get("error") else "completed")
        )
        await run_repo.update_status(run.id, final_status, initial_state.get("error"))

        if final_status == "completed":
            await run_repo.mark_completed(run.id)
            _running_states.pop(run_id, None)
        elif final_status == "failed":
            _running_states.pop(run_id, None)

        await session.commit()

        return RunResponse(
            run_id=run_id,
            objective=run.objective,
            status=final_status,
            created_at=run.created_at.isoformat(),
            plan_steps=initial_state.get("plan_steps"),
            current_step=initial_state.get("current_step"),
            error=initial_state.get("error"),
        )
    except Exception as e:
        logger.error(f"Run {run_id} failed: {e}")
        initial_state["error"] = str(e)
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

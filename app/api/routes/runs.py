import uuid
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.agents.graph import build_flowpilot_graph
from src.agents.state import AgentState
from src.config.settings import Settings

logger = logging.getLogger(__name__)
router = APIRouter()

_active_runs: dict[str, dict] = {}


class CreateRunRequest(BaseModel):
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


@router.post("/runs", response_model=RunResponse)
async def create_run(request: CreateRunRequest):
    run_id = str(uuid.uuid4())

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

    _active_runs[run_id] = {
        "state": initial_state,
        "status": "planning",
        "created_at": datetime.utcnow().isoformat(),
    }

    logger.info(f"Created run {run_id}: {request.objective[:80]}")

    try:
        graph = build_flowpilot_graph()

        result = None
        async for step_output in graph.astream(initial_state):
            result = step_output
            node_name = list(step_output.keys())[0] if step_output else "unknown"
            logger.info(f"Run {run_id}: completed step '{node_name}'")

            if isinstance(step_output, dict):
                for node_state in step_output.values():
                    if isinstance(node_state, dict):
                        _active_runs[run_id]["state"].update(node_state)

            current = _active_runs[run_id]["state"].get("current_step", "")
            if current == "awaiting_approval":
                _active_runs[run_id]["status"] = "awaiting_approval"
                break

        final_state = _active_runs[run_id]["state"]
        if final_state.get("current_step") != "awaiting_approval":
            _active_runs[run_id]["status"] = "completed" if not final_state.get("error") else "failed"

        return RunResponse(
            run_id=run_id,
            objective=request.objective,
            status=_active_runs[run_id]["status"],
            created_at=_active_runs[run_id]["created_at"],
            plan_steps=final_state.get("plan_steps"),
            current_step=final_state.get("current_step"),
            error=final_state.get("error"),
        )

    except Exception as e:
        logger.error(f"Run {run_id} failed: {e}")
        _active_runs[run_id]["status"] = "failed"
        _active_runs[run_id]["state"]["error"] = str(e)
        raise HTTPException(status_code=500, detail=f"Run failed: {str(e)}")


@router.get("/runs")
async def list_runs():
    return [
        {
            "run_id": run_id,
            "objective": data["state"].get("objective", ""),
            "status": data["status"],
            "created_at": data["created_at"],
            "current_step": data["state"].get("current_step"),
        }
        for run_id, data in _active_runs.items()
    ]


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str):
    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="Run not found")

    data = _active_runs[run_id]
    state = data["state"]

    return RunResponse(
        run_id=run_id,
        objective=state.get("objective", ""),
        status=data["status"],
        created_at=data["created_at"],
        plan_steps=state.get("plan_steps"),
        current_step=state.get("current_step"),
        error=state.get("error"),
    )


@router.get("/runs/{run_id}/status")
async def get_run_status(run_id: str):
    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="Run not found")

    data = _active_runs[run_id]
    state = data["state"]

    return {
        "run_id": run_id,
        "status": data["status"],
        "current_step": state.get("current_step"),
        "error": state.get("error"),
        "transactions_count": len(state.get("transactions", [])),
        "candidates_count": len(state.get("scored_candidates", [])),
        "has_audit_report": state.get("audit_report") is not None,
    }

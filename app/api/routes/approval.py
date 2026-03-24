import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.agents.graph import build_flowpilot_graph

logger = logging.getLogger(__name__)
router = APIRouter()


class ApprovalRequest(BaseModel):
    candidate_ids: list[str]


class RejectionRequest(BaseModel):
    candidate_ids: list[str]
    reason: Optional[str] = None


@router.get("/runs/{run_id}/candidates")
async def get_candidates(run_id: str):
    from app.api.routes.runs import _active_runs

    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="Run not found")

    state = _active_runs[run_id]["state"]
    candidates = state.get("scored_candidates", [])

    return {
        "run_id": run_id,
        "total": len(candidates),
        "candidates": candidates,
        "status": _active_runs[run_id]["status"],
    }


@router.post("/runs/{run_id}/approve")
async def approve_candidates(run_id: str, request: ApprovalRequest):
    from app.api.routes.runs import _active_runs

    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="Run not found")

    data = _active_runs[run_id]
    if data["status"] != "awaiting_approval":
        raise HTTPException(status_code=400, detail=f"Run is not awaiting approval (status: {data['status']})")

    state = data["state"]
    state["approved_candidate_ids"] = request.candidate_ids
    state["current_step"] = "approved"
    data["status"] = "executing"

    logger.info(f"Run {run_id}: approved {len(request.candidate_ids)} candidates, resuming execution")

    try:
        graph = build_flowpilot_graph()

        async for step_output in graph.astream(state):
            for node_state in step_output.values():
                if isinstance(node_state, dict):
                    state.update(node_state)

        data["status"] = "completed" if not state.get("error") else "failed"

        return {
            "run_id": run_id,
            "status": data["status"],
            "approved_count": len(request.candidate_ids),
            "current_step": state.get("current_step"),
        }
    except Exception as e:
        logger.error(f"Run {run_id} execution failed after approval: {e}")
        data["status"] = "failed"
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/runs/{run_id}/reject")
async def reject_candidates(run_id: str, request: RejectionRequest):
    from app.api.routes.runs import _active_runs

    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="Run not found")

    data = _active_runs[run_id]
    state = data["state"]

    state["rejected_candidate_ids"] = request.candidate_ids
    logger.info(f"Run {run_id}: rejected {len(request.candidate_ids)} candidates")

    remaining_approved = [
        cid for cid in state.get("approved_candidate_ids", [])
        if cid not in request.candidate_ids
    ]
    state["approved_candidate_ids"] = remaining_approved

    return {
        "run_id": run_id,
        "rejected_count": len(request.candidate_ids),
        "remaining_approved": len(remaining_approved),
    }

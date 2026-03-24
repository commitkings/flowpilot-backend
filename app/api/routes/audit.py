import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/runs/{run_id}/report")
async def get_audit_report(run_id: str):
    from app.api.routes.runs import _active_runs

    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="Run not found")

    state = _active_runs[run_id]["state"]
    report = state.get("audit_report")

    if not report:
        raise HTTPException(status_code=404, detail="Audit report not yet generated")

    return report


@router.get("/runs/{run_id}/report/download")
async def download_audit_report(run_id: str):
    from app.api.routes.runs import _active_runs

    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="Run not found")

    state = _active_runs[run_id]["state"]
    report = state.get("audit_report")

    if not report:
        raise HTTPException(status_code=404, detail="Audit report not yet generated")

    report_json = json.dumps(report, indent=2, default=str)

    return JSONResponse(
        content=report,
        headers={
            "Content-Disposition": f'attachment; filename="flowpilot_report_{run_id}.json"',
        },
    )

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import AuditRepository

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _serialize_audit_log(entry) -> dict:
    return {
        "id": entry.id,
        "run_id": str(entry.run_id),
        "step_id": str(entry.step_id) if entry.step_id else None,
        "agent_type": entry.agent_type,
        "action": entry.action,
        "detail": entry.detail,
        "api_endpoint": entry.api_endpoint,
        "request_hash": entry.request_hash,
        "response_status": entry.response_status,
        "response_time_ms": entry.response_time_ms,
        "created_at": entry.created_at.isoformat(),
    }


@router.get("/runs/{run_id}/report")
async def get_audit_report(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    run_uuid = _parse_uuid(run_id, "run_id")
    audit_repo = AuditRepository(session)
    entries = await audit_repo.get_by_run(run_uuid)

    if not entries:
        raise HTTPException(status_code=404, detail="Audit report not yet generated")

    return {
        "run_id": run_id,
        "entries": [_serialize_audit_log(entry) for entry in entries],
    }


@router.get("/runs/{run_id}/report/download")
async def download_audit_report(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    run_uuid = _parse_uuid(run_id, "run_id")
    audit_repo = AuditRepository(session)
    entries = await audit_repo.get_by_run(run_uuid)

    if not entries:
        raise HTTPException(status_code=404, detail="Audit report not yet generated")

    payload = {
        "run_id": run_id,
        "entries": [_serialize_audit_log(entry) for entry in entries],
    }

    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="flowpilot_report_{run_id}.json"',
        },
    )

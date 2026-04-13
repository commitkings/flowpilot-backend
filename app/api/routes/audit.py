import logging
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import BusinessMemberModel
from src.infrastructure.database.repositories import AuditRepository

logger = logging.getLogger(__name__)
router = APIRouter()


class AuditExportEmailRequest(BaseModel):
    email: EmailStr
    entries: list[dict]
    format: str = "csv"
    pdf_base64: Optional[str] = None


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
        "created_at": entry.created_at.isoformat(),
    }


def _extract_structured_report(entries: list) -> Optional[dict]:
    """Find the final_report audit entry and return its structured detail."""
    for entry in reversed(entries):
        if entry.action == "final_report" and entry.detail:
            return entry.detail
    return None


@router.get("/runs/{run_id}/report")
async def get_audit_report(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    run_uuid = _parse_uuid(run_id, "run_id")
    audit_repo = AuditRepository(session)
    entries = await audit_repo.get_by_run(run_uuid)

    if not entries:
        raise HTTPException(status_code=404, detail="Audit report not yet generated")

    structured = _extract_structured_report(entries)
    all_entries = [_serialize_audit_log(entry) for entry in entries]

    if structured:
        return {
            "run_id": run_id,
            "report": structured,
            "audit_trail": [
                _serialize_audit_log(e) for e in entries if e.action != "final_report"
            ],
            "entries": all_entries,
        }

    return {
        "run_id": run_id,
        "entries": all_entries,
    }


@router.get("/runs/{run_id}/report/download")
async def download_audit_report(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    run_uuid = _parse_uuid(run_id, "run_id")
    audit_repo = AuditRepository(session)
    entries = await audit_repo.get_by_run(run_uuid)

    if not entries:
        raise HTTPException(status_code=404, detail="Audit report not yet generated")

    all_entries = [_serialize_audit_log(entry) for entry in entries]
    structured = _extract_structured_report(entries)

    if structured:
        payload = {
            "run_id": run_id,
            "report": structured,
            "audit_trail": [
                _serialize_audit_log(e) for e in entries if e.action != "final_report"
            ],
            "entries": all_entries,
        }
    else:
        payload = {
            "run_id": run_id,
            "entries": all_entries,
        }

    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="flowpilot_report_{run_id}.json"',
        },
    )


# ------------------------------------------------------------------
# Global audit trail (Gap 3)
# ------------------------------------------------------------------


@router.get("/audit")
async def list_audit_entries(
    run_id: Optional[str] = Query(None),
    agent_type: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    # Scope audit log to caller's organisation
    membership_result = await session.execute(
        select(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id
        )
    )
    membership = membership_result.scalars().first()
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No business membership found",
        )

    run_uuid = _parse_uuid(run_id, "run_id") if run_id else None
    audit_repo = AuditRepository(session)
    entries, total = await audit_repo.list_all(
        business_id=membership.business_id,
        run_id=run_uuid,
        agent_type=agent_type,
        action=action,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )
    return {
        "entries": [_serialize_audit_log(e) for e in entries],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/audit/export-email")
async def export_audit_email(
    body: AuditExportEmailRequest,
    current_user=Depends(get_current_user),
):
    """Send audit log entries to an email address as a CSV or PDF attachment."""
    from src.services.email_service import send_audit_export_email

    if not body.entries:
        raise HTTPException(status_code=400, detail="No audit entries to export.")

    if body.format == "pdf" and not body.pdf_base64:
        raise HTTPException(status_code=400, detail="pdf_base64 is required when format is 'pdf'.")

    exported_by = current_user.display_name or current_user.email or "FlowPilot User"
    sent = await send_audit_export_email(
        to=body.email,
        exported_by=exported_by,
        entries=body.entries,
        fmt=body.format,
        pdf_base64=body.pdf_base64,
    )
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send export email. Please try again.")
    return {"message": f"Audit log export sent to {body.email}"}

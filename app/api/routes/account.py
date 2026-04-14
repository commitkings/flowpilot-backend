"""
Account actions — data export and account deletion.
Both actions are restricted to the business owner.
"""

import json
from datetime import datetime, timezone
from typing import Optional

import pyotp
from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth.dependencies import get_current_user
from src.config.settings import Settings
from src.infrastructure.cache import account_delete_store
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    AgentRunModel,
    BusinessMemberModel,
    BusinessModel,
    ReconciledTransactionModel,
)
from src.infrastructure.database.repositories import AuditRepository
from src.infrastructure.database.repositories.user_repository import UserRepository
from src.services.email_service import send_account_deletion_code_email

router = APIRouter(prefix="/account", tags=["account"])


class DeleteAccountRequest(BaseModel):
    totp_code: Optional[str] = None
    delete_code: Optional[str] = None


async def _require_owner(session: AsyncSession, user_id) -> BusinessMemberModel:
    """Fetch the caller's membership and raise 403 if they are not an owner."""
    result = await session.execute(
        select(BusinessMemberModel)
        .options(selectinload(BusinessMemberModel.business))
        .where(BusinessMemberModel.user_id == user_id)
    )
    membership = result.scalars().first()
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No business membership found",
        )
    if membership.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the business owner can perform this action",
        )
    return membership


@router.post("/export")
async def export_account_data(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Generate a full JSON export of the workspace (GDPR-style). Owner only."""
    membership = await _require_owner(session, current_user.id)
    business_id = membership.business_id

    repo = UserRepository(session)
    all_memberships = await repo.get_memberships(current_user.id)

    # ── Business profile ──────────────────────────────────────────────────────
    biz = membership.business
    business_data = (
        {
            "id": str(biz.id),
            "business_name": biz.business_name,
            "business_type": biz.business_type,
            "rc_number": biz.rc_number,
            "tax_id": biz.tax_id,
            "city": biz.city,
            "state": biz.state,
            "country": biz.country,
            "phone": biz.phone,
            "website": biz.website,
            "created_at": biz.created_at.isoformat() if biz.created_at else None,
        }
        if biz
        else None
    )

    # ── Team members ──────────────────────────────────────────────────────────
    members_rows = (
        await session.execute(
            select(BusinessMemberModel)
            .options(selectinload(BusinessMemberModel.user))
            .where(BusinessMemberModel.business_id == business_id)
        )
    ).scalars().all()

    team_members = [
        {
            "user_id": str(m.user_id),
            "role": m.role,
            "is_active": getattr(m, "is_active", True),
            "email": m.user.email if m.user else None,
            "display_name": m.user.display_name if m.user else None,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        }
        for m in members_rows
    ]

    # ── Runs (most recent 500) ────────────────────────────────────────────────
    runs_rows = (
        await session.execute(
            select(AgentRunModel)
            .where(AgentRunModel.business_id == business_id)
            .order_by(AgentRunModel.created_at.desc())
            .limit(500)
        )
    ).scalars().all()

    runs = [
        {
            "id": str(r.id),
            "objective": r.objective,
            "state": r.state,
            "total_amount": float(r.total_amount) if r.total_amount else None,
            "candidate_count": r.candidate_count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in runs_rows
    ]

    # ── Transactions (most recent 500) ────────────────────────────────────────
    txn_rows = (
        await session.execute(
            select(ReconciledTransactionModel)
            .where(ReconciledTransactionModel.business_id == business_id)
            .order_by(ReconciledTransactionModel.transaction_timestamp.desc())
            .limit(500)
        )
    ).scalars().all()

    transactions = [
        {
            "id": str(t.id),
            "reference": t.interswitch_ref,
            "counterparty_name": t.counterparty_name,
            "counterparty_bank": t.counterparty_bank,
            "amount": float(t.amount) if t.amount else None,
            "currency": t.currency,
            "direction": t.direction,
            "status": t.status,
            "narration": t.narration,
            "transaction_date": t.transaction_timestamp.isoformat() if t.transaction_timestamp else None,
        }
        for t in txn_rows
    ]

    # ── Audit logs (most recent 500) ─────────────────────────────────────────
    audit_repo = AuditRepository(session)
    audit_rows, _ = await audit_repo.list_all(business_id=business_id, limit=500)

    audit_logs = [
        {
            "id": a.id,
            "run_id": str(a.run_id),
            "step_id": str(a.step_id) if a.step_id else None,
            "agent_type": a.agent_type,
            "action": a.action,
            "detail": a.detail,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in audit_rows
    ]

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {
            "id": str(current_user.id),
            "email": current_user.email,
            "display_name": current_user.display_name,
            "first_name": current_user.first_name,
            "last_name": current_user.last_name,
            "job_title": current_user.job_title,
            "phone": current_user.phone,
            "timezone": current_user.timezone,
            "department": current_user.department,
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
            "last_login_at": current_user.last_login_at.isoformat() if current_user.last_login_at else None,
        },
        "memberships": [
            {"business_id": str(m.business_id), "role": m.role}
            for m in all_memberships
        ],
        "business": business_data,
        "team_members": team_members,
        "runs": runs,
        "transactions": transactions,
        "audit_logs": audit_logs,
    }

    content = json.dumps(export, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="flowpilot-export-{current_user.id}.json"'
        },
    )


@router.post("/request-delete-code")
async def request_delete_code(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Send a 6-digit deletion confirmation code to the owner's email.

    Only applicable when the owner does not have 2FA enabled.
    If 2FA is enabled, the client should use the TOTP code instead.
    Owner only.
    """
    await _require_owner(session, current_user.id)

    if getattr(current_user, "totp_enabled_at", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use your authenticator app code — 2FA is enabled on this account",
        )

    code = account_delete_store.generate_code()
    await account_delete_store.save(str(current_user.id), code)

    await send_account_deletion_code_email(
        to=current_user.email,
        display_name=current_user.display_name or current_user.email,
        code=code,
        frontend_url=Settings.FRONTEND_URL,
    )

    return {"message": "Verification code sent to your email"}


@router.delete("/delete")
async def delete_account(
    body: DeleteAccountRequest = Body(default_factory=DeleteAccountRequest),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Permanently deactivate the owner's account and their business. Owner only.

    Requires verification:
    - If 2FA is enabled: provide a valid TOTP code via `totp_code`
    - If 2FA is not enabled: provide the email code sent via /account/request-delete-code
    """
    membership = await _require_owner(session, current_user.id)
    business_id = membership.business_id

    # ── Verify identity ───────────────────────────────────────────────────────
    if getattr(current_user, "totp_enabled_at", None) is not None:
        if not body.totp_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Two-factor authentication code required",
            )
        totp = pyotp.TOTP(current_user.totp_secret)
        if not totp.verify(body.totp_code, valid_window=1):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid authentication code",
            )
    else:
        if not body.delete_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email verification code required",
            )
        ok = await account_delete_store.verify(str(current_user.id), body.delete_code)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired verification code",
            )

    # ── Soft-delete ───────────────────────────────────────────────────────────
    biz = (
        await session.execute(
            select(BusinessModel).where(BusinessModel.id == business_id)
        )
    ).scalars().first()
    if biz:
        biz.is_active = False

    members = (
        await session.execute(
            select(BusinessMemberModel).where(
                BusinessMemberModel.business_id == business_id
            )
        )
    ).scalars().all()
    for m in members:
        m.is_active = False

    current_user.is_active = False

    await session.commit()
    return {"status": "deleted", "message": "Account and workspace have been deactivated."}

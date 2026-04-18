"""
Account actions — data export, import, and account/org deletion.
"""

import json
from datetime import datetime, timezone
from typing import Optional

import pyotp
from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile, status
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
    AiCreditTransactionModel,
    ApiKeyModel,
    ApprovalRuleModel,
    BlocklistEntryModel,
    BusinessConfigModel,
    BusinessMemberModel,
    BusinessModel,
    IndividualKycSubmissionModel,
    KycSubmissionModel,
    ReconciledTransactionModel,
    SavedRecipientModel,
    ScheduledRunModel,
    WalletModel,
    WalletTransactionModel,
    WebhookModel,
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
            .options(selectinload(AgentRunModel.payout_candidates))
            .where(AgentRunModel.business_id == business_id)
            .order_by(AgentRunModel.created_at.desc())
            .limit(500)
        )
    ).scalars().all()

    runs = []
    for run in runs_rows:
        pcs = run.payout_candidates or []
        cand_count = len(pcs)
        total_amt = float(sum(pc.amount for pc in pcs)) if pcs else None
        runs.append(
            {
                "id": str(run.id),
                "objective": run.objective,
                "status": run.status,
                "total_amount": total_amt,
                "candidate_count": cand_count,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            }
        )

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

    # ── Business config ───────────────────────────────────────────────────────
    biz_config = (
        await session.execute(
            select(BusinessConfigModel).where(BusinessConfigModel.business_id == business_id)
        )
    ).scalars().first()

    business_config_data = None
    if biz_config:
        business_config_data = {
            "monthly_txn_volume_range": biz_config.monthly_txn_volume_range,
            "avg_monthly_payouts_range": biz_config.avg_monthly_payouts_range,
            "primary_bank": biz_config.primary_bank,
            "primary_use_cases": biz_config.primary_use_cases,
            "risk_appetite": biz_config.risk_appetite,
            "default_risk_tolerance": str(biz_config.default_risk_tolerance) if biz_config.default_risk_tolerance is not None else None,
            "default_budget_cap": str(biz_config.default_budget_cap) if biz_config.default_budget_cap is not None else None,
            "daily_payout_limit": str(biz_config.daily_payout_limit) if biz_config.daily_payout_limit is not None else None,
            "single_payout_cap": str(biz_config.single_payout_cap) if biz_config.single_payout_cap is not None else None,
            "risk_alert_threshold": str(biz_config.risk_alert_threshold) if biz_config.risk_alert_threshold is not None else None,
            "liquidity_alert_buffer": str(biz_config.liquidity_alert_buffer) if biz_config.liquidity_alert_buffer is not None else None,
            "require_2fa": biz_config.require_2fa,
            "preferences": biz_config.preferences or {},
        }

    # ── Saved recipients ──────────────────────────────────────────────────────
    saved_recipients_rows = (
        await session.execute(
            select(SavedRecipientModel)
            .where(SavedRecipientModel.business_id == business_id)
            .order_by(SavedRecipientModel.created_at.desc())
        )
    ).scalars().all()

    saved_recipients = [
        {
            "name": r.name,
            "account_number": r.account_number,
            "institution_code": r.institution_code,
            "email": r.email,
            "notes": r.notes,
            "tags": r.tags or [],
        }
        for r in saved_recipients_rows
    ]

    # ── API keys (metadata only — no secrets) ────────────────────────────────
    api_keys_rows = (
        await session.execute(
            select(ApiKeyModel)
            .where(ApiKeyModel.business_id == business_id)
            .order_by(ApiKeyModel.created_at.desc())
        )
    ).scalars().all()

    api_keys_data = [
        {
            "name": k.name,
            "key_prefix": k.key_prefix,
            "scopes": k.scopes or [],
            "expires_at": k.expires_at.isoformat() if k.expires_at else None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
            "created_at": k.created_at.isoformat() if k.created_at else None,
        }
        for k in api_keys_rows
    ]

    # ── Webhooks (no signing secrets) ─────────────────────────────────────────
    webhooks_rows = (
        await session.execute(
            select(WebhookModel)
            .where(WebhookModel.business_id == business_id)
            .order_by(WebhookModel.created_at.desc())
        )
    ).scalars().all()

    webhooks_data = [
        {
            "url": wh.url,
            "events": wh.events or [],
            "is_active": wh.is_active,
            "created_at": wh.created_at.isoformat() if wh.created_at else None,
        }
        for wh in webhooks_rows
    ]

    # ── Approval rules ────────────────────────────────────────────────────────
    approval_rules_rows = (
        await session.execute(
            select(ApprovalRuleModel)
            .where(ApprovalRuleModel.business_id == business_id)
            .order_by(ApprovalRuleModel.created_at.desc())
        )
    ).scalars().all()

    approval_rules_data = [
        {
            "name": r.name,
            "condition": r.condition,
            "threshold": str(r.threshold),
            "required_approvers": r.required_approvers,
            "approver_roles": r.approver_roles or [],
            "is_active": r.is_active,
        }
        for r in approval_rules_rows
    ]

    # ── Blocklist entries (active only) ───────────────────────────────────────
    blocklist_rows = (
        await session.execute(
            select(BlocklistEntryModel)
            .where(
                BlocklistEntryModel.business_id == business_id,
                BlocklistEntryModel.is_active.is_(True),
            )
            .order_by(BlocklistEntryModel.created_at.desc())
        )
    ).scalars().all()

    blocklist_data = [
        {
            "type": e.type,
            "value": e.value,
            "reason": e.reason,
        }
        for e in blocklist_rows
    ]

    # ── Scheduled runs ────────────────────────────────────────────────────────
    scheduled_runs_rows = (
        await session.execute(
            select(ScheduledRunModel)
            .where(ScheduledRunModel.business_id == business_id)
            .order_by(ScheduledRunModel.created_at.desc())
        )
    ).scalars().all()

    scheduled_runs_data = [
        {
            "name": sr.name,
            "objective": sr.objective,
            "run_type": sr.run_type,
            "cron_expression": sr.cron_expression,
            "frequency_label": sr.frequency_label,
            "is_active": sr.is_active,
            "run_config": sr.run_config or {},
            "created_at": sr.created_at.isoformat() if sr.created_at else None,
        }
        for sr in scheduled_runs_rows
    ]

    # ── KYC status ────────────────────────────────────────────────────────────
    kyc_submission = (
        await session.execute(
            select(KycSubmissionModel).where(KycSubmissionModel.business_id == business_id)
        )
    ).scalars().first()

    kyc_data = None
    if kyc_submission:
        kyc_data = {
            "status": kyc_submission.status,
            "business_type": kyc_submission.business_type,
            "registration_number": kyc_submission.registration_number,
            "submitted_at": kyc_submission.submitted_at.isoformat() if getattr(kyc_submission, "submitted_at", None) else None,
            "verified_at": kyc_submission.verified_at.isoformat() if getattr(kyc_submission, "verified_at", None) else None,
        }

    individual_kyc = (
        await session.execute(
            select(IndividualKycSubmissionModel).where(IndividualKycSubmissionModel.business_id == business_id)
        )
    ).scalars().first()

    individual_kyc_data = None
    if individual_kyc:
        individual_kyc_data = {
            "level_1_type": individual_kyc.level_1_type,
            "level_1_status": individual_kyc.level_1_status,
            "level_2_status": individual_kyc.level_2_status,
            "level_3_status": individual_kyc.level_3_status,
        }

    # ── Wallet ────────────────────────────────────────────────────────────────
    wallet = (
        await session.execute(
            select(WalletModel).where(WalletModel.business_id == business_id)
        )
    ).scalars().first()

    wallet_data = None
    if wallet:
        recent_txns = (
            await session.execute(
                select(WalletTransactionModel)
                .where(WalletTransactionModel.business_id == business_id)
                .order_by(WalletTransactionModel.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
        wallet_data = {
            "balance": str(wallet.balance),
            "currency": wallet.currency,
            "recent_transactions": [
                {
                    "type": t.type,
                    "amount": str(t.amount),
                    "description": t.description,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in recent_txns
            ],
        }

    # ── AI credits balance ────────────────────────────────────────────────────
    from sqlalchemy import func as _func
    credits_result = await session.execute(
        select(
            _func.coalesce(_func.sum(AiCreditTransactionModel.credits).filter(
                AiCreditTransactionModel.type == "purchase"
            ), 0).label("purchased"),
            _func.coalesce(_func.sum(AiCreditTransactionModel.credits).filter(
                AiCreditTransactionModel.type == "debit"
            ), 0).label("spent"),
        ).where(AiCreditTransactionModel.business_id == business_id)
    )
    credits_row = credits_result.first()
    ai_credits_balance = int(credits_row.purchased) - int(credits_row.spent) if credits_row else 0

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
            "notification_preferences": getattr(current_user, "notification_preferences", None) or {},
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
            "last_login_at": current_user.last_login_at.isoformat() if current_user.last_login_at else None,
        },
        "memberships": [
            {"business_id": str(m.business_id), "role": m.role}
            for m in all_memberships
        ],
        "business": business_data,
        "business_config": business_config_data,
        "team_members": team_members,
        "saved_recipients": saved_recipients,
        "api_keys": api_keys_data,
        "webhooks": webhooks_data,
        "approval_rules": approval_rules_data,
        "blocklist_entries": blocklist_data,
        "scheduled_runs": scheduled_runs_data,
        "kyc": kyc_data,
        "individual_kyc": individual_kyc_data,
        "wallet": wallet_data,
        "ai_credits_balance": ai_credits_balance,
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
    """Send a 6-digit deletion confirmation code to the caller's email.

    Works for both owners (organisation deletion) and non-owners (self-deletion).
    Only applicable when the caller does not have 2FA enabled — if they do,
    they should use their TOTP code instead.
    """
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

    # ── Wallet must be empty before deletion ──────────────────────────────────
    wallet_check = (
        await session.execute(
            select(WalletModel).where(WalletModel.business_id == business_id)
        )
    ).scalars().first()
    if wallet_check and wallet_check.balance > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Your wallet has a balance of ₦{wallet_check.balance:,.2f}. Please withdraw all funds before deleting your organisation.",
        )

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
    _now = datetime.now(timezone.utc)

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

    # Revoke all API keys
    api_keys = (
        await session.execute(
            select(ApiKeyModel).where(
                ApiKeyModel.business_id == business_id,
                ApiKeyModel.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    for k in api_keys:
        k.revoked_at = _now

    # Deactivate all webhooks
    webhooks = (
        await session.execute(
            select(WebhookModel).where(
                WebhookModel.business_id == business_id,
                WebhookModel.is_active.is_(True),
            )
        )
    ).scalars().all()
    for wh in webhooks:
        wh.is_active = False

    # Pause all scheduled runs
    sched_runs = (
        await session.execute(
            select(ScheduledRunModel).where(
                ScheduledRunModel.business_id == business_id,
                ScheduledRunModel.is_active.is_(True),
            )
        )
    ).scalars().all()
    for sr in sched_runs:
        sr.is_active = False

    current_user.is_active = False

    await session.commit()
    return {"status": "deleted", "message": "Account and workspace have been deactivated."}


# ─────────────────────────────────────────────────────────────────────────────
# Self-deletion for non-owners
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/delete-self")
async def delete_self(
    body: DeleteAccountRequest = Body(default_factory=DeleteAccountRequest),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Allow a non-owner member to delete their own account.

    Requires identity verification:
    - If 2FA is enabled: provide a valid TOTP code via `totp_code`
    - If 2FA is not enabled: provide the email code sent via /account/request-delete-code

    Removes their business membership and deactivates their user account.
    Historical activity (runs, audit logs) is preserved.
    Owners cannot use this endpoint — they must delete the organisation instead.
    """
    # Check if caller is an owner — owners must use the /delete endpoint
    membership = (
        await session.execute(
            select(BusinessMemberModel).where(
                BusinessMemberModel.user_id == current_user.id
            )
        )
    ).scalars().first()

    if membership and membership.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="As an organisation owner, you must delete the organisation instead of your individual account.",
        )

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

    # Remove the membership record
    if membership:
        await session.delete(membership)

    # Deactivate the user
    current_user.is_active = False

    await session.commit()
    return {"status": "deleted", "message": "Your account has been deactivated."}


# ─────────────────────────────────────────────────────────────────────────────
# Data import
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/import")
async def import_account_data(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Import a previously exported workspace JSON to restore business and profile data.

    Restores:
    - Business profile (name, type, city, state, country, phone, website)
    - Owner's personal profile (display_name, first_name, last_name, job_title, phone, timezone, department)
    - Team members (non-owner members are re-invited if not already in the org)

    Runs, transactions and audit logs are NOT imported (operational data).
    Owner only.
    """
    membership = await _require_owner(session, current_user.id)
    business_id = membership.business_id

    if file.content_type not in ("application/json", "text/plain", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="File must be a JSON export from FlowPilot")

    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")

    if "exported_at" not in data or "business" not in data:
        raise HTTPException(
            status_code=400,
            detail="This does not appear to be a valid FlowPilot export file",
        )

    restored: list[str] = []

    # ── Restore business profile ──────────────────────────────────────────────
    biz_data = data.get("business") or {}
    if biz_data:
        biz = (
            await session.execute(
                select(BusinessModel).where(BusinessModel.id == business_id)
            )
        ).scalars().first()

        if biz:
            for field in ("business_name", "business_type", "rc_number", "tax_id", "city", "state", "country", "phone", "website"):
                val = biz_data.get(field)
                if val and not getattr(biz, field, None):
                    setattr(biz, field, val)
            restored.append("business_profile")

    # ── Restore owner profile ─────────────────────────────────────────────────
    user_data = data.get("user") or {}
    if user_data:
        for field in ("display_name", "first_name", "last_name", "job_title", "phone", "timezone", "department"):
            val = user_data.get(field)
            if val and not getattr(current_user, field, None):
                setattr(current_user, field, val)
        restored.append("owner_profile")

    # ── Re-invite team members ────────────────────────────────────────────────
    team_members = data.get("team_members") or []
    invited_count = 0
    skipped_count = 0

    if team_members:
        from src.infrastructure.database.repositories.invitation_repository import InvitationRepository
        from src.infrastructure.database.repositories.user_repository import UserRepository as _UR
        from src.services.email_service import send_team_invite_email, send_team_added_email
        from app.api.auth.passwords import normalize_email

        invite_repo = InvitationRepository(session)
        user_repo = _UR(session)
        business_name = biz_data.get("business_name") or "your organisation"
        inviter_name = current_user.display_name or current_user.email
        dashboard_url = f"{Settings.FRONTEND_URL}/dashboard"

        for m in team_members:
            email = m.get("email")
            role = m.get("role", "analyst")

            if not email or role == "owner":
                continue

            try:
                normalized = normalize_email(email)
            except Exception:
                continue

            if normalized == normalize_email(current_user.email):
                continue

            target_user = await user_repo.get_by_email(normalized)
            if target_user:
                from sqlalchemy import and_
                existing = (
                    await session.execute(
                        select(BusinessMemberModel).where(
                            and_(
                                BusinessMemberModel.business_id == business_id,
                                BusinessMemberModel.user_id == target_user.id,
                            )
                        )
                    )
                ).scalars().first()
                if existing:
                    skipped_count += 1
                    continue

                member = BusinessMemberModel(
                    business_id=business_id,
                    user_id=target_user.id,
                    role=role if role in ("approver", "analyst") else "analyst",
                    joined_at=datetime.now(timezone.utc),
                )
                session.add(member)
                await session.flush()
                await send_team_added_email(
                    to=target_user.email,
                    business_name=business_name,
                    inviter_name=inviter_name,
                    role=role,
                    dashboard_url=dashboard_url,
                    frontend_url=Settings.FRONTEND_URL,
                )
                invited_count += 1
            else:
                existing_invite = await invite_repo.get_pending_for_business(business_id, normalized)
                if existing_invite:
                    skipped_count += 1
                    continue

                invite = await invite_repo.create(
                    business_id=business_id,
                    invited_email=normalized,
                    role=role if role in ("approver", "analyst") else "analyst",
                    invited_by_user_id=current_user.id,
                )
                accept_url = f"{Settings.FRONTEND_URL}{Settings.ACCEPT_INVITE_PATH}?token={invite.token}"
                await send_team_invite_email(
                    to=normalized,
                    business_name=business_name,
                    inviter_name=inviter_name,
                    role=role,
                    accept_url=accept_url,
                    frontend_url=Settings.FRONTEND_URL,
                )
                invited_count += 1

        if invited_count > 0:
            restored.append(f"team_members ({invited_count} re-invited, {skipped_count} skipped)")

    # ── Restore business config ───────────────────────────────────────────────
    biz_config_data = data.get("business_config") or {}
    if biz_config_data:
        biz_config = (
            await session.execute(
                select(BusinessConfigModel).where(BusinessConfigModel.business_id == business_id)
            )
        ).scalars().first()
        if biz_config:
            _config_fields = (
                "monthly_txn_volume_range", "avg_monthly_payouts_range", "primary_bank",
                "primary_use_cases", "risk_appetite",
                "default_budget_cap", "daily_payout_limit", "single_payout_cap",
                "risk_alert_threshold", "liquidity_alert_buffer",
            )
            for field in _config_fields:
                val = biz_config_data.get(field)
                if val is not None and not getattr(biz_config, field, None):
                    setattr(biz_config, field, val)
            _rt = biz_config_data.get("default_risk_tolerance")
            if _rt is not None and biz_config.default_risk_tolerance == 0.35:
                biz_config.default_risk_tolerance = _rt
            restored.append("business_config")

    # ── Restore saved recipients ──────────────────────────────────────────────
    saved_recipients = data.get("saved_recipients") or []
    if saved_recipients:
        from sqlalchemy import and_ as _and
        recipients_added = 0
        for r in saved_recipients:
            acct = r.get("account_number")
            inst = r.get("institution_code")
            name = r.get("name")
            if not acct or not inst or not name:
                continue
            dup = (
                await session.execute(
                    select(SavedRecipientModel).where(
                        _and(
                            SavedRecipientModel.business_id == business_id,
                            SavedRecipientModel.account_number == acct,
                            SavedRecipientModel.institution_code == inst,
                        )
                    )
                )
            ).scalars().first()
            if dup:
                continue
            session.add(SavedRecipientModel(
                business_id=business_id,
                name=name,
                account_number=acct,
                institution_code=inst,
                email=r.get("email"),
                notes=r.get("notes"),
                tags=r.get("tags") or [],
            ))
            recipients_added += 1
        if recipients_added:
            restored.append(f"saved_recipients ({recipients_added} restored)")

    # ── Restore approval rules ────────────────────────────────────────────────
    approval_rules = data.get("approval_rules") or []
    if approval_rules:
        _valid_conditions = {"amount_above", "risk_score_above", "always"}
        rules_added = 0
        for r in approval_rules:
            condition = r.get("condition")
            if condition not in _valid_conditions:
                continue
            session.add(ApprovalRuleModel(
                business_id=business_id,
                name=r.get("name") or "Imported rule",
                condition=condition,
                threshold=r.get("threshold") or 0,
                required_approvers=max(1, int(r.get("required_approvers") or 1)),
                approver_roles=r.get("approver_roles") or ["approver"],
                is_active=bool(r.get("is_active", True)),
            ))
            rules_added += 1
        if rules_added:
            restored.append(f"approval_rules ({rules_added} restored)")

    # ── Restore blocklist entries ─────────────────────────────────────────────
    blocklist_entries = data.get("blocklist_entries") or []
    if blocklist_entries:
        _valid_types = {"account_number", "beneficiary_name", "bank_code"}
        from sqlalchemy import and_ as _and2
        blocklist_added = 0
        for e in blocklist_entries:
            etype = e.get("type")
            evalue = e.get("value")
            if etype not in _valid_types or not evalue:
                continue
            dup = (
                await session.execute(
                    select(BlocklistEntryModel).where(
                        _and2(
                            BlocklistEntryModel.business_id == business_id,
                            BlocklistEntryModel.type == etype,
                            BlocklistEntryModel.value == evalue,
                            BlocklistEntryModel.is_active.is_(True),
                        )
                    )
                )
            ).scalars().first()
            if dup:
                continue
            session.add(BlocklistEntryModel(
                business_id=business_id,
                type=etype,
                value=evalue,
                reason=e.get("reason") or "",
                is_active=True,
            ))
            blocklist_added += 1
        if blocklist_added:
            restored.append(f"blocklist_entries ({blocklist_added} restored)")

    # ── Restore scheduled runs (imported as paused) ───────────────────────────
    scheduled_runs = data.get("scheduled_runs") or []
    if scheduled_runs:
        runs_added = 0
        for sr in scheduled_runs:
            name = sr.get("name")
            objective = sr.get("objective")
            if not name or not objective:
                continue
            session.add(ScheduledRunModel(
                business_id=business_id,
                created_by=current_user.id,
                name=name,
                objective=objective,
                run_type=sr.get("run_type") or "recurring",
                cron_expression=sr.get("cron_expression"),
                frequency_label=sr.get("frequency_label") or "Custom",
                is_active=False,
                run_config=sr.get("run_config") or {},
            ))
            runs_added += 1
        if runs_added:
            restored.append(f"scheduled_runs ({runs_added} restored, paused)")

    # ── Restore webhooks (imported as inactive) ───────────────────────────────
    webhooks_to_restore = data.get("webhooks") or []
    if webhooks_to_restore:
        import secrets as _secrets
        from app.api.auth.passwords import hash_password as _hp
        webhooks_added = 0
        for wh in webhooks_to_restore:
            url = wh.get("url")
            if not url:
                continue
            raw_secret = f"whsec_{_secrets.token_hex(32)}"
            session.add(WebhookModel(
                business_id=business_id,
                created_by=current_user.id,
                url=url,
                events=wh.get("events") or [],
                is_active=False,
                secret_hash=_hp(raw_secret),
                signing_secret=raw_secret,
            ))
            webhooks_added += 1
        if webhooks_added:
            restored.append(f"webhooks ({webhooks_added} restored, inactive)")

    # ── Restore notification preferences ─────────────────────────────────────
    notif_prefs = (data.get("user") or {}).get("notification_preferences")
    if notif_prefs and isinstance(notif_prefs, dict):
        existing_prefs = dict(getattr(current_user, "notification_preferences", None) or {})
        if not existing_prefs:
            current_user.notification_preferences = notif_prefs
            restored.append("notification_preferences")

    await session.commit()

    return {
        "status": "imported",
        "restored": restored,
        "exported_at": data.get("exported_at"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Notification preferences
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_PREFS = {
    "login_alerts": True,
    "security_alerts": True,
    "payout_updates": True,
    "kyc_updates": True,
    "api_key_warnings": True,
    "wallet_alerts": True,
    "scheduled_run_reminders": True,
}

_ALLOWED_PREF_KEYS = set(_DEFAULT_PREFS.keys())


class NotificationPreferencesUpdate(BaseModel):
    login_alerts: Optional[bool] = None
    security_alerts: Optional[bool] = None
    payout_updates: Optional[bool] = None
    kyc_updates: Optional[bool] = None
    api_key_warnings: Optional[bool] = None
    wallet_alerts: Optional[bool] = None
    scheduled_run_reminders: Optional[bool] = None


@router.get("/notification-preferences")
async def get_notification_preferences(
    current_user=Depends(get_current_user),
):
    """Return the current user's notification preferences (merged with defaults)."""
    stored = getattr(current_user, "notification_preferences", None) or {}
    return {**_DEFAULT_PREFS, **stored}


@router.patch("/notification-preferences")
async def update_notification_preferences(
    body: NotificationPreferencesUpdate,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Update one or more notification preference toggles."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one preference must be provided",
        )

    existing = dict(getattr(current_user, "notification_preferences", None) or {})
    existing.update(updates)
    current_user.notification_preferences = existing
    current_user.updated_at = datetime.now(timezone.utc)
    await session.commit()

    return {**_DEFAULT_PREFS, **existing}

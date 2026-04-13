"""Team member management routes.

CRUD for BusinessMemberModel — list, invite, update role, remove.
Only the business *owner* can invite, modify, or remove members.
"""

import logging
import uuid
from datetime import datetime, timezone as tz
from typing import Optional

import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth.dependencies import get_current_user
from app.api.auth.passwords import normalize_email
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    UserModel,
)
from src.infrastructure.database.repositories.invitation_repository import (
    InvitationRepository,
)
from src.infrastructure.database.repositories.user_repository import UserRepository
from src.services.email_service import send_team_added_email, send_team_invite_email
from src.infrastructure.database.repositories.notification_repository import NotificationRepository

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_ROLES = {"owner", "approver", "analyst"}
INVITE_ROLES = {"approver", "analyst"}  # owners cannot be invited


# ── helpers ──────────────────────────────────────────────────────────────────


async def _get_caller_membership(
    session: AsyncSession, user_id: uuid.UUID
) -> BusinessMemberModel:
    result = await session.execute(
        select(BusinessMemberModel)
        .options(selectinload(BusinessMemberModel.business))
        .where(BusinessMemberModel.user_id == user_id)
    )
    membership = result.scalars().first()
    if not membership:
        raise HTTPException(status_code=403, detail="No business membership found")
    return membership


def _require_owner(membership: BusinessMemberModel) -> None:
    if membership.role != "owner":
        raise HTTPException(
            status_code=403, detail="Only the business owner can perform this action"
        )


def _serialize_member(member: BusinessMemberModel, user: Optional[UserModel]) -> dict:
    return {
        "id": str(member.id),
        "user_id": str(member.user_id),
        "role": member.role,
        "is_active": getattr(member, "is_active", True),
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "created_at": member.created_at.isoformat(),
        "user": {
            "display_name": user.display_name if user else None,
            "email": user.email if user else None,
            "avatar_url": user.avatar_url if user else None,
        }
        if user
        else None,
    }


# ── request bodies ────────────────────────────────────────────────────────────


class InviteMemberRequest(BaseModel):
    email: EmailStr
    role: str = "analyst"


class UpdateMemberRoleRequest(BaseModel):
    role: str


class UpdateMemberStatusRequest(BaseModel):
    is_active: bool


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("/team/members")
async def list_team_members(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    caller = await _get_caller_membership(session, current_user.id)

    from sqlalchemy import func

    total = (
        await session.execute(
            select(func.count())
            .select_from(BusinessMemberModel)
            .where(BusinessMemberModel.business_id == caller.business_id)
        )
    ).scalar() or 0

    rows = list(
        (
            await session.execute(
                select(BusinessMemberModel)
                .options(selectinload(BusinessMemberModel.user))
                .where(BusinessMemberModel.business_id == caller.business_id)
                .order_by(BusinessMemberModel.created_at)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return {
        "members": [_serialize_member(m, m.user) for m in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/team/invite")
async def invite_member(
    body: InviteMemberRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """Invite a team member by email.

    - Existing user → creates BusinessMember immediately, sends notification email.
    - New user      → creates pending Invitation with a magic link, sends invite email.
    """
    caller = await _get_caller_membership(session, current_user.id)
    _require_owner(caller)

    role = body.role.lower()
    if role not in INVITE_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {sorted(INVITE_ROLES)}",
        )

    normalized = normalize_email(str(body.email))

    # Owner cannot invite themselves
    if normalized == normalize_email(current_user.email):
        raise HTTPException(
            status_code=400, detail="You cannot invite yourself to your own team"
        )

    user_repo = UserRepository(session)
    invite_repo = InvitationRepository(session)

    target_user = await user_repo.get_by_email(normalized)
    business_name = caller.business.business_name if caller.business else "your organisation"
    inviter_name = current_user.display_name or current_user.email

    # ── Scenario A: user already registered ──────────────────────────────────
    if target_user:
        existing = (
            await session.execute(
                select(BusinessMemberModel).where(
                    and_(
                        BusinessMemberModel.business_id == caller.business_id,
                        BusinessMemberModel.user_id == target_user.id,
                    )
                )
            )
        ).scalars().first()

        if existing:
            raise HTTPException(
                status_code=409, detail="User is already a member of this team"
            )

        member = BusinessMemberModel(
            business_id=caller.business_id,
            user_id=target_user.id,
            role=role,
            joined_at=datetime.now(tz.utc),
        )
        session.add(member)
        await session.flush()

        # Send notification email (best-effort)
        dashboard_url = f"{Settings.FRONTEND_URL}/dashboard"
        await send_team_added_email(
            to=target_user.email,
            business_name=business_name,
            inviter_name=inviter_name,
            role=role,
            dashboard_url=dashboard_url,
            frontend_url=Settings.FRONTEND_URL,
        )

        # In-app notification for the added user
        try:
            notif_repo = NotificationRepository(session)
            await notif_repo.create(
                user_id=target_user.id,
                business_id=caller.business_id,
                title="Added to team",
                message=f"{inviter_name} added you to {business_name} as {role.capitalize()}.",
                type="info",
                resource_type="team",
                resource_id=str(caller.business_id),
            )
            await session.flush()
        except Exception as _notif_exc:
            logger.warning("Failed to create team notification: %s", _notif_exc)

        return {
            "status": "added",
            "member": _serialize_member(member, target_user),
        }

    # ── Scenario B: new user — create pending invitation ─────────────────────
    existing_invite = await invite_repo.get_pending_for_business(
        caller.business_id, normalized
    )
    if existing_invite:
        raise HTTPException(
            status_code=409,
            detail="An invite has already been sent to this email address",
        )

    invite = await invite_repo.create(
        business_id=caller.business_id,
        invited_email=normalized,
        role=role,
        invited_by_user_id=current_user.id,
    )

    accept_url = (
        f"{Settings.FRONTEND_URL}{Settings.ACCEPT_INVITE_PATH}?token={invite.token}"
    )
    await send_team_invite_email(
        to=normalized,
        business_name=business_name,
        inviter_name=inviter_name,
        role=role,
        accept_url=accept_url,
        frontend_url=Settings.FRONTEND_URL,
    )

    return {
        "status": "invited",
        "invite_id": str(invite.id),
        "invited_email": normalized,
    }


@router.get("/team/invite/{token}")
async def get_invite_details(
    token: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Public endpoint — returns invite metadata for the accept-invite page."""
    invite_repo = InvitationRepository(session)
    invite = await invite_repo.get_by_token(token)

    if not invite:
        raise HTTPException(status_code=404, detail="Invitation not found")

    if invite.status == "accepted":
        return {
            "status": "accepted",
            "business_name": invite.business.business_name if invite.business else None,
            "invited_email": invite.invited_email,
            "role": invite.role,
            "inviter_name": None,
        }

    if invite.status == "expired" or invite.expires_at < datetime.now(tz.utc):
        if invite.status != "expired":
            await invite_repo.mark_expired(invite)
        return {
            "status": "expired",
            "business_name": invite.business.business_name if invite.business else None,
            "invited_email": invite.invited_email,
            "role": invite.role,
            "inviter_name": None,
        }

    inviter_name: Optional[str] = None
    if invite.invited_by:
        inviter_name = invite.invited_by.display_name or invite.invited_by.email

    return {
        "status": "pending",
        "business_name": invite.business.business_name if invite.business else None,
        "invited_email": invite.invited_email,
        "role": invite.role,
        "inviter_name": inviter_name,
        "expires_at": invite.expires_at.isoformat(),
    }


@router.post("/team/accept-invite/{token}")
async def accept_invite(
    token: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """For an already-registered user who receives an invite and wants to join.

    Validates the invite token, checks the current user's email matches, then
    creates the BusinessMember record.
    """
    invite_repo = InvitationRepository(session)
    invite = await invite_repo.get_by_token(token)

    if not invite:
        raise HTTPException(status_code=404, detail="Invitation not found")

    if invite.status != "pending" or invite.expires_at < datetime.now(tz.utc):
        raise HTTPException(status_code=410, detail="Invitation has expired or already been used")

    if current_user.email != invite.invited_email:
        raise HTTPException(
            status_code=403,
            detail="This invitation was sent to a different email address",
        )

    existing = (
        await session.execute(
            select(BusinessMemberModel).where(
                and_(
                    BusinessMemberModel.business_id == invite.business_id,
                    BusinessMemberModel.user_id == current_user.id,
                )
            )
        )
    ).scalars().first()

    if existing:
        raise HTTPException(status_code=409, detail="You are already a member of this team")

    member = BusinessMemberModel(
        business_id=invite.business_id,
        user_id=current_user.id,
        role=invite.role,
        joined_at=datetime.now(tz.utc),
    )
    session.add(member)
    await invite_repo.mark_accepted(invite)

    return {
        "status": "accepted",
        "business_id": str(invite.business_id),
        "role": invite.role,
    }


@router.patch("/team/members/{member_id}")
async def update_member_role(
    member_id: str,
    body: UpdateMemberRoleRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    caller = await _get_caller_membership(session, current_user.id)
    _require_owner(caller)

    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}"
        )

    try:
        mid = uuid.UUID(member_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid member ID")

    target = (
        await session.execute(
            select(BusinessMemberModel)
            .options(selectinload(BusinessMemberModel.user))
            .where(
                BusinessMemberModel.id == mid,
                BusinessMemberModel.business_id == caller.business_id,
            )
        )
    ).scalars().first()

    if not target:
        raise HTTPException(status_code=404, detail="Team member not found")

    if target.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    target.role = body.role
    await session.flush()

    return {"status": "updated", "member": _serialize_member(target, target.user)}


@router.patch("/team/members/{member_id}/status")
async def toggle_member_status(
    member_id: str,
    body: UpdateMemberStatusRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """Enable or disable a team member's access without removing them."""
    caller = await _get_caller_membership(session, current_user.id)
    _require_owner(caller)

    try:
        mid = uuid.UUID(member_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid member ID")

    target = (
        await session.execute(
            select(BusinessMemberModel)
            .options(selectinload(BusinessMemberModel.user))
            .where(
                BusinessMemberModel.id == mid,
                BusinessMemberModel.business_id == caller.business_id,
            )
        )
    ).scalars().first()

    if not target:
        raise HTTPException(status_code=404, detail="Team member not found")

    if target.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot disable your own account")

    if target.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot disable another owner")

    target.is_active = body.is_active
    await session.flush()

    return {"status": "updated", "member": _serialize_member(target, target.user)}


@router.delete("/team/members/{member_id}")
async def remove_member(
    member_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    caller = await _get_caller_membership(session, current_user.id)
    _require_owner(caller)

    try:
        mid = uuid.UUID(member_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid member ID")

    target = (
        await session.execute(
            select(BusinessMemberModel).where(
                BusinessMemberModel.id == mid,
                BusinessMemberModel.business_id == caller.business_id,
            )
        )
    ).scalars().first()

    if not target:
        raise HTTPException(status_code=404, detail="Team member not found")

    if target.user_id == current_user.id:
        raise HTTPException(
            status_code=400, detail="Cannot remove yourself from the team"
        )

    await session.delete(target)
    await session.flush()

    return {"status": "removed"}


# ── Bulk import ───────────────────────────────────────────────────────────────


@router.get("/team/import/template")
async def download_import_template():
    """Return a CSV template for bulk team member import.

    Columns: email, role, first_name (optional), last_name (optional)
    Roles:   analyst | approver
    """
    rows = [
        ["email", "role", "first_name", "last_name"],
        ["alice@example.com", "analyst", "Alice", "Smith"],
        ["bob@example.com", "approver", "Bob", "Jones"],
    ]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=team_import_template.csv"},
    )


@router.post("/team/import")
async def bulk_import_members(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """Import multiple team members from a CSV file.

    Only the business owner can use this endpoint.
    CSV must have a header row with at minimum: email, role.
    Roles must be 'analyst' or 'approver' (owners cannot be imported).

    Returns a summary of added, invited, skipped, and failed rows.
    """
    caller = await _get_caller_membership(session, current_user.id)
    _require_owner(caller)

    if file.content_type not in ("text/csv", "application/vnd.ms-excel", "text/plain"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # strip BOM if present
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "email" not in [f.lower().strip() for f in reader.fieldnames]:
        raise HTTPException(
            status_code=400,
            detail="CSV must have an 'email' column. Download the template to see the expected format.",
        )

    # Normalize header names to lowercase
    def _col(row: dict, name: str) -> str:
        for k, v in row.items():
            if k.lower().strip() == name:
                return (v or "").strip()
        return ""

    user_repo = UserRepository(session)
    invite_repo = InvitationRepository(session)
    business_name = caller.business.business_name if caller.business else "your organisation"
    inviter_name = current_user.display_name or current_user.email
    dashboard_url = f"{Settings.FRONTEND_URL}/dashboard"

    results: list[dict] = []
    added = invited = skipped = failed = 0

    for line_num, row in enumerate(reader, start=2):
        email_raw = _col(row, "email")
        role_raw = _col(row, "role") or "analyst"

        if not email_raw:
            results.append({"line": line_num, "status": "error", "reason": "Missing email"})
            failed += 1
            continue

        try:
            normalized = normalize_email(email_raw)
        except Exception:
            results.append({"line": line_num, "email": email_raw, "status": "error", "reason": "Invalid email format"})
            failed += 1
            continue

        role = role_raw.lower()
        if role not in INVITE_ROLES:
            results.append({
                "line": line_num,
                "email": normalized,
                "status": "error",
                "reason": f"Invalid role '{role_raw}'. Must be: analyst or approver",
            })
            failed += 1
            continue

        # Check if already a member
        target_user = await user_repo.get_by_email(normalized)
        if target_user:
            existing = (
                await session.execute(
                    select(BusinessMemberModel).where(
                        and_(
                            BusinessMemberModel.business_id == caller.business_id,
                            BusinessMemberModel.user_id == target_user.id,
                        )
                    )
                )
            ).scalars().first()

            if existing:
                results.append({"line": line_num, "email": normalized, "status": "skipped", "reason": "Already a member"})
                skipped += 1
                continue

            # Add existing user directly
            member = BusinessMemberModel(
                business_id=caller.business_id,
                user_id=target_user.id,
                role=role,
                joined_at=datetime.now(tz.utc),
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
            results.append({"line": line_num, "email": normalized, "status": "added", "role": role})
            added += 1

        else:
            # Check for existing pending invite
            existing_invite = await invite_repo.get_pending_for_business(caller.business_id, normalized)
            if existing_invite:
                results.append({"line": line_num, "email": normalized, "status": "skipped", "reason": "Invitation already pending"})
                skipped += 1
                continue

            invite = await invite_repo.create(
                business_id=caller.business_id,
                invited_email=normalized,
                role=role,
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
            results.append({"line": line_num, "email": normalized, "status": "invited", "role": role})
            invited += 1

    await session.commit()

    return {
        "summary": {"added": added, "invited": invited, "skipped": skipped, "failed": failed, "total": added + invited + skipped + failed},
        "results": results,
    }

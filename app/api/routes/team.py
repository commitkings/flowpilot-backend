"""Team member management routes (Gap 5).

CRUD for BusinessMemberModel — list, invite, update role, remove.
Only the business *owner* can invite, modify, or remove members.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    UserModel,
)
from src.infrastructure.database.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_ROLES = {"owner", "approver", "analyst"}


# ---- helpers ----------------------------------------------------------------


async def _get_caller_membership(
    session: AsyncSession, user_id: uuid.UUID
) -> BusinessMemberModel:
    """Return the caller's first business membership or 403."""
    result = await session.execute(
        select(BusinessMemberModel).where(BusinessMemberModel.user_id == user_id)
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


# ---- request bodies ---------------------------------------------------------


class InviteMemberRequest(BaseModel):
    email: EmailStr
    role: str = "analyst"


class UpdateMemberRoleRequest(BaseModel):
    role: str


# ---- routes -----------------------------------------------------------------


@router.get("/team/members")
async def list_team_members(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    caller = await _get_caller_membership(session, current_user.id)

    base = (
        select(BusinessMemberModel)
        .options(selectinload(BusinessMemberModel.user))
        .where(BusinessMemberModel.business_id == caller.business_id)
    )

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
                base.order_by(BusinessMemberModel.created_at)
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
    caller = await _get_caller_membership(session, current_user.id)
    _require_owner(caller)

    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}")

    user_repo = UserRepository(session)
    target_user = await user_repo.get_by_email(body.email)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found. They must register first.")

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
        raise HTTPException(status_code=409, detail="User is already a member of this team")

    from datetime import datetime, timezone as tz

    member = BusinessMemberModel(
        business_id=caller.business_id,
        user_id=target_user.id,
        role=body.role,
        joined_at=datetime.now(tz.utc),
    )
    session.add(member)
    await session.flush()

    return {
        "status": "invited",
        "member": _serialize_member(member, target_user),
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
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}")

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
        raise HTTPException(status_code=400, detail="Cannot remove yourself from the team")

    await session.delete(target)
    await session.flush()

    return {"status": "removed"}

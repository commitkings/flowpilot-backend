"""
Org configuration routes — Approval Rules and Blocklist.

Approval rules require owner role.
Blocklist also requires owner role.

Endpoints:
    GET    /org/approval-rules
    POST   /org/approval-rules
    PATCH  /org/approval-rules/{id}
    DELETE /org/approval-rules/{id}

    GET    /org/blocklist
    POST   /org/blocklist
    PATCH  /org/blocklist/{id}
    DELETE /org/blocklist/{id}
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    ApprovalRuleModel,
    BlocklistEntryModel,
    BusinessMemberModel,
)

router = APIRouter(tags=["org-config"])

VALID_CONDITIONS = {"amount_above", "risk_score_above", "always"}
VALID_BLOCKLIST_TYPES = {"account_number", "beneficiary_name", "bank_code"}


async def _get_business_id(current_user, session: AsyncSession) -> uuid.UUID:
    result = await session.execute(
        select(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id
        )
    )
    membership = result.scalars().first()
    if not membership:
        raise HTTPException(status_code=403, detail="No business membership found")
    return membership.business_id


# =========================================================================== #
# Approval Rules
# =========================================================================== #

class CreateApprovalRuleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    condition: str
    threshold: float = Field(0.0, ge=0)
    required_approvers: int = Field(1, ge=1)
    approver_roles: list[str] = Field(default_factory=lambda: ["approver"])
    is_active: bool = True


class PatchApprovalRuleRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    condition: Optional[str] = None
    threshold: Optional[float] = None
    required_approvers: Optional[int] = Field(None, ge=1)
    approver_roles: Optional[list[str]] = None
    is_active: Optional[bool] = None


def _serialize_rule(r: ApprovalRuleModel) -> dict:
    return {
        "id": str(r.id),
        "name": r.name,
        "condition": r.condition,
        "threshold": float(r.threshold),
        "required_approvers": r.required_approvers,
        "approver_roles": list(r.approver_roles or []),
        "is_active": r.is_active,
    }


@router.get("/org/approval-rules")
async def list_approval_rules(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(ApprovalRuleModel)
        .where(ApprovalRuleModel.business_id == business_id)
        .order_by(ApprovalRuleModel.created_at.asc())
    )
    rules = result.scalars().all()
    return {"rules": [_serialize_rule(r) for r in rules]}


@router.post("/org/approval-rules", status_code=status.HTTP_201_CREATED)
async def create_approval_rule(
    body: CreateApprovalRuleRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
    session: AsyncSession = Depends(get_db_session),
):
    if body.condition not in VALID_CONDITIONS:
        raise HTTPException(
            status_code=422,
            detail=f"condition must be one of: {', '.join(sorted(VALID_CONDITIONS))}",
        )

    business_id = await _get_business_id(current_user, session)

    # Safety check: if the rule requires more than 1 approver, ensure the business
    # has enough members with owner or approver role to satisfy the requirement.
    if body.required_approvers > 1:
        capable_count = (await session.execute(
            select(func.count()).select_from(BusinessMemberModel).where(
                BusinessMemberModel.business_id == business_id,
                BusinessMemberModel.role.in_(["owner", "approver"]),
                BusinessMemberModel.is_active == True,
            )
        )).scalar_one()
        if capable_count < body.required_approvers:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"This rule requires {body.required_approvers} approver(s), but your team "
                    f"only has {capable_count} member(s) with the owner or approver role. "
                    "Please invite additional team members before enabling this rule."
                ),
            )

    rule = ApprovalRuleModel(
        business_id=business_id,
        name=body.name,
        condition=body.condition,
        threshold=body.threshold,
        required_approvers=body.required_approvers,
        approver_roles=body.approver_roles,
        is_active=body.is_active,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return _serialize_rule(rule)


@router.patch("/org/approval-rules/{rule_id}")
async def update_approval_rule(
    rule_id: uuid.UUID,
    body: PatchApprovalRuleRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
    session: AsyncSession = Depends(get_db_session),
):
    if body.condition is not None and body.condition not in VALID_CONDITIONS:
        raise HTTPException(status_code=422, detail=f"Invalid condition: {body.condition}")

    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(ApprovalRuleModel).where(
            ApprovalRuleModel.id == rule_id,
            ApprovalRuleModel.business_id == business_id,
        )
    )
    rule = result.scalars().first()
    if not rule:
        raise HTTPException(status_code=404, detail="Approval rule not found")

    # Safety check: if increasing required_approvers, ensure enough capable members exist
    new_required = body.required_approvers if body.required_approvers is not None else rule.required_approvers
    if new_required > 1:
        capable_count = (await session.execute(
            select(func.count()).select_from(BusinessMemberModel).where(
                BusinessMemberModel.business_id == business_id,
                BusinessMemberModel.role.in_(["owner", "approver"]),
                BusinessMemberModel.is_active == True,
            )
        )).scalar_one()
        if capable_count < new_required:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"This rule requires {new_required} approver(s), but your team "
                    f"only has {capable_count} member(s) with the owner or approver role. "
                    "Please invite additional team members before enabling this rule."
                ),
            )

    values: dict = {"updated_at": datetime.now(timezone.utc)}
    if body.name is not None:
        values["name"] = body.name
    if body.condition is not None:
        values["condition"] = body.condition
    if body.threshold is not None:
        values["threshold"] = body.threshold
    if body.required_approvers is not None:
        values["required_approvers"] = body.required_approvers
    if body.approver_roles is not None:
        values["approver_roles"] = body.approver_roles
    if body.is_active is not None:
        values["is_active"] = body.is_active

    await session.execute(
        update(ApprovalRuleModel).where(ApprovalRuleModel.id == rule_id).values(**values)
    )
    await session.commit()
    await session.refresh(rule)
    return _serialize_rule(rule)


@router.delete("/org/approval-rules/{rule_id}")
async def delete_approval_rule(
    rule_id: uuid.UUID,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(ApprovalRuleModel).where(
            ApprovalRuleModel.id == rule_id,
            ApprovalRuleModel.business_id == business_id,
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Approval rule not found")

    await session.execute(
        delete(ApprovalRuleModel).where(ApprovalRuleModel.id == rule_id)
    )
    await session.commit()
    return {"status": "deleted"}


# =========================================================================== #
# Blocklist
# =========================================================================== #

class CreateBlocklistEntryRequest(BaseModel):
    type: str
    value: str = Field(..., min_length=1, max_length=255)
    reason: str = Field("", max_length=1000)


class PatchBlocklistEntryRequest(BaseModel):
    is_active: Optional[bool] = None
    reason: Optional[str] = Field(None, max_length=1000)


def _serialize_blocklist(e: BlocklistEntryModel, added_by_email: Optional[str] = None) -> dict:
    return {
        "id": str(e.id),
        "type": e.type,
        "value": e.value,
        "reason": e.reason,
        "added_by": added_by_email or str(e.added_by) if e.added_by else None,
        "is_active": e.is_active,
        "created_at": e.created_at.isoformat(),
    }


@router.get("/org/blocklist")
async def list_blocklist(
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
    search: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    business_id = await _get_business_id(current_user, session)

    base = select(BlocklistEntryModel).where(
        BlocklistEntryModel.business_id == business_id
    )
    if type:
        base = base.where(BlocklistEntryModel.type == type)
    if search:
        base = base.where(BlocklistEntryModel.value.ilike(f"%{search}%"))

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    rows = (
        await session.execute(
            base.order_by(BlocklistEntryModel.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    return {
        "entries": [_serialize_blocklist(e) for e in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/org/blocklist", status_code=status.HTTP_201_CREATED)
async def create_blocklist_entry(
    body: CreateBlocklistEntryRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    if body.type not in VALID_BLOCKLIST_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"type must be one of: {', '.join(sorted(VALID_BLOCKLIST_TYPES))}",
        )

    business_id = await _get_business_id(current_user, session)

    entry = BlocklistEntryModel(
        business_id=business_id,
        added_by=current_user.id,
        type=body.type,
        value=body.value,
        reason=body.reason,
        is_active=True,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return _serialize_blocklist(entry, added_by_email=current_user.email)


@router.patch("/org/blocklist/{entry_id}")
async def update_blocklist_entry(
    entry_id: uuid.UUID,
    body: PatchBlocklistEntryRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(BlocklistEntryModel).where(
            BlocklistEntryModel.id == entry_id,
            BlocklistEntryModel.business_id == business_id,
        )
    )
    entry = result.scalars().first()
    if not entry:
        raise HTTPException(status_code=404, detail="Blocklist entry not found")

    values: dict = {}
    if body.is_active is not None:
        values["is_active"] = body.is_active
    if body.reason is not None:
        values["reason"] = body.reason
    if values:
        await session.execute(
            update(BlocklistEntryModel).where(BlocklistEntryModel.id == entry_id).values(**values)
        )
        await session.commit()
        await session.refresh(entry)

    return _serialize_blocklist(entry)


@router.delete("/org/blocklist/{entry_id}")
async def delete_blocklist_entry(
    entry_id: uuid.UUID,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(BlocklistEntryModel).where(
            BlocklistEntryModel.id == entry_id,
            BlocklistEntryModel.business_id == business_id,
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Blocklist entry not found")

    await session.execute(
        delete(BlocklistEntryModel).where(BlocklistEntryModel.id == entry_id)
    )
    await session.commit()
    return {"status": "deleted"}

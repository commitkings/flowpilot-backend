"""
Saved recipients — address book of reusable beneficiaries per business.

Endpoints:
    GET    /recipients
    POST   /recipients
    PATCH  /recipients/{recipient_id}
    DELETE /recipients/{recipient_id}
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    SavedRecipientModel,
)

router = APIRouter(tags=["recipients"])


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _serialize(r: SavedRecipientModel) -> dict:
    return {
        "id": str(r.id),
        "business_id": str(r.business_id),
        "name": r.name,
        "account_number": r.account_number,
        "institution_code": r.institution_code,
        "email": r.email,
        "notes": r.notes,
        "tags": r.tags or [],
        "payment_count": r.payment_count,
        "last_paid_at": r.last_paid_at.isoformat() if r.last_paid_at else None,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateRecipientRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    account_number: str = Field(..., min_length=1, max_length=32)
    institution_code: str = Field(..., min_length=1, max_length=16)
    email: Optional[str] = Field(None, max_length=256)
    notes: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class UpdateRecipientRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=256)
    account_number: Optional[str] = Field(None, min_length=1, max_length=32)
    institution_code: Optional[str] = Field(None, min_length=1, max_length=16)
    email: Optional[str] = Field(None, max_length=256)
    notes: Optional[str] = None
    tags: Optional[List[str]] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/recipients")
async def list_recipients(
    search: Optional[str] = Query(None, max_length=256),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)

    query = select(SavedRecipientModel).where(
        SavedRecipientModel.business_id == business_id
    )

    if search:
        term = f"%{search}%"
        query = query.where(
            or_(
                SavedRecipientModel.name.ilike(term),
                SavedRecipientModel.account_number.ilike(term),
            )
        )

    query = query.order_by(SavedRecipientModel.name.asc()).limit(limit).offset(offset)
    result = await session.execute(query)
    recipients = result.scalars().all()

    return {
        "recipients": [_serialize(r) for r in recipients],
        "total": len(recipients),
        "limit": limit,
        "offset": offset,
    }


@router.post("/recipients", status_code=status.HTTP_201_CREATED)
async def create_recipient(
    body: CreateRecipientRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "analyst")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)

    recipient = SavedRecipientModel(
        business_id=business_id,
        name=body.name,
        account_number=body.account_number,
        institution_code=body.institution_code,
        email=body.email,
        notes=body.notes,
        tags=body.tags,
    )
    session.add(recipient)
    await session.commit()
    await session.refresh(recipient)
    return _serialize(recipient)


@router.patch("/recipients/{recipient_id}")
async def update_recipient(
    recipient_id: uuid.UUID,
    body: UpdateRecipientRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "analyst")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)

    result = await session.execute(
        select(SavedRecipientModel).where(
            SavedRecipientModel.id == recipient_id,
            SavedRecipientModel.business_id == business_id,
        )
    )
    recipient = result.scalars().first()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")

    values: dict = {"updated_at": datetime.now(timezone.utc)}
    if body.name is not None:
        values["name"] = body.name
    if body.account_number is not None:
        values["account_number"] = body.account_number
    if body.institution_code is not None:
        values["institution_code"] = body.institution_code
    if body.email is not None:
        values["email"] = body.email
    if body.notes is not None:
        values["notes"] = body.notes
    if body.tags is not None:
        values["tags"] = body.tags

    await session.execute(
        update(SavedRecipientModel)
        .where(SavedRecipientModel.id == recipient_id)
        .values(**values)
    )
    await session.commit()
    await session.refresh(recipient)
    return _serialize(recipient)


@router.delete("/recipients/{recipient_id}")
async def delete_recipient(
    recipient_id: uuid.UUID,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "analyst")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)

    result = await session.execute(
        select(SavedRecipientModel).where(
            SavedRecipientModel.id == recipient_id,
            SavedRecipientModel.business_id == business_id,
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Recipient not found")

    await session.execute(
        delete(SavedRecipientModel).where(SavedRecipientModel.id == recipient_id)
    )
    await session.commit()
    return {"status": "deleted"}

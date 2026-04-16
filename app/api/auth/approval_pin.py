"""Approval step-up PIN — lets users set a 4-6 digit PIN that must be entered
before confirming a payout run approval.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.passwords import hash_pin, verify_password
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import UserModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/approval-pin", tags=["approval-pin"])


class SetPinRequest(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6, pattern=r"^\d{4,6}$")


class VerifyPinRequest(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6, pattern=r"^\d{4,6}$")


@router.get("/status")
async def get_pin_status(current_user=Depends(get_current_user)):
    """Return whether the current user has set up an approval PIN."""
    return {"has_pin": bool(current_user.approval_pin_hash)}


@router.post("/setup", status_code=status.HTTP_200_OK)
async def setup_pin(
    body: SetPinRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Create or replace the approval PIN."""
    hashed = hash_pin(body.pin)
    await session.execute(
        update(UserModel)
        .where(UserModel.id == current_user.id)
        .values(approval_pin_hash=hashed)
    )
    await session.commit()
    return {"message": "Approval PIN set successfully."}


@router.post("/verify", status_code=status.HTTP_200_OK)
async def verify_pin(
    body: VerifyPinRequest,
    current_user=Depends(get_current_user),
):
    """Verify the approval PIN. Returns 200 on success, 400 on wrong PIN."""
    if not current_user.approval_pin_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No approval PIN configured. Set one up in Settings → Security.",
        )
    if not verify_password(body.pin, current_user.approval_pin_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect approval PIN.",
        )
    return {"message": "PIN verified."}


@router.delete("/remove", status_code=status.HTTP_200_OK)
async def remove_pin(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Remove the approval PIN."""
    await session.execute(
        update(UserModel)
        .where(UserModel.id == current_user.id)
        .values(approval_pin_hash=None)
    )
    await session.commit()
    return {"message": "Approval PIN removed."}

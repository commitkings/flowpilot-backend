"""Payee self-service portal API (see docs/SCHEMA_REDESIGN_AND_PAYEE_PORTAL.md)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import UserModel
from src.infrastructure.database.flowpilot_models import (
    PayeeBankAccountModel,
    PayeeProfileModel,
    UserProfileModel,
)
from src.services.payment_service import PaymentService

router = APIRouter(prefix="/payee", tags=["payee"])


class PayeeRegisterRequest(BaseModel):
    email: EmailStr
    account_number: str = Field(..., min_length=8, max_length=20)
    institution_code: str = Field(..., min_length=3, max_length=10)
    display_name: str = Field(default="Payee", max_length=100)


class PayeeRegisterResponse(BaseModel):
    user_id: str
    payee_profile_id: str
    bank_account_id: str
    message: str


@router.post("/register", response_model=PayeeRegisterResponse, status_code=201)
async def register_payee(
    body: PayeeRegisterRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a payee user, profile, and bank account row after BAV.

    Does not send email verification yet — extend with OTP / magic link as in the doc.
    """
    normalized = body.email.strip().lower()

    existing = await session.execute(select(UserModel).where(UserModel.email == normalized))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    svc = PaymentService()
    validation = await svc.validate_account(
        account_number=body.account_number.strip(),
        bank_code=body.institution_code.strip(),
    )
    account_name = validation.account_name or ""

    user = UserModel(
        email=normalized,
        account_type="payee",
        external_id=f"payee:{uuid.uuid4()}",
        is_active=True,
    )
    session.add(user)
    await session.flush()

    session.add(
        UserProfileModel(
            user_id=user.id,
            display_name=body.display_name.strip() or "Payee",
        )
    )
    await session.flush()

    profile = PayeeProfileModel(
        user_id=user.id,
        display_name=body.display_name.strip() or "Payee",
        tier=1,
    )
    session.add(profile)
    await session.flush()

    bank = PayeeBankAccountModel(
        account_number=body.account_number.strip(),
        institution_code=body.institution_code.strip(),
        account_name=account_name,
        is_bav_verified=True,
        payee_profile_id=profile.id,
    )
    session.add(bank)
    await session.flush()

    await session.commit()

    return PayeeRegisterResponse(
        user_id=str(user.id),
        payee_profile_id=str(profile.id),
        bank_account_id=str(bank.id),
        message="Payee registered. Wire email verification and JWT claims for payee sessions.",
    )


class PayeeProfileOut(BaseModel):
    id: str
    display_name: str
    business_name: str | None
    tier: int
    kyc_status: str


@router.get("/profile", response_model=PayeeProfileOut)
async def get_payee_profile(
    session: AsyncSession = Depends(get_db_session),
    current_user: UserModel = Depends(get_current_user),
):
    if getattr(current_user, "account_type", "payer") != "payee":
        raise HTTPException(status_code=403, detail="Payee portal access only")

    result = await session.execute(
        select(PayeeProfileModel).where(PayeeProfileModel.user_id == current_user.id)
    )
    p = result.scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Payee profile not found")

    return PayeeProfileOut(
        id=str(p.id),
        display_name=p.display_name,
        business_name=p.business_name,
        tier=int(p.tier),
        kyc_status=p.kyc_status,
    )

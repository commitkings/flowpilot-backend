"""
Organisation / business profile routes.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.business_repository import (
    BusinessRepository,
)

router = APIRouter(prefix="/org", tags=["org"])


class UpdateOrgRequest(BaseModel):
    business_name: Optional[str] = None
    business_type: Optional[str] = None
    rc_number: Optional[str] = None
    tax_id: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    website: Optional[str] = None
    phone: Optional[str] = None


class UpdateOrgConfigRequest(BaseModel):
    monthly_txn_volume_range: Optional[str] = None
    avg_monthly_payouts_range: Optional[str] = None
    primary_bank: Optional[str] = None
    primary_use_cases: Optional[list[str]] = None
    risk_appetite: Optional[str] = None
    default_risk_tolerance: Optional[float] = None
    default_budget_cap: Optional[float] = None


async def _get_user_business_id(current_user, session) -> uuid.UUID:
    """Resolve the business ID from the user's first membership."""
    from src.infrastructure.database.repositories.user_repository import UserRepository

    repo = UserRepository(session)
    memberships = await repo.get_memberships(current_user.id)
    if not memberships:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User does not belong to any organisation",
        )
    return memberships[0].business_id


@router.get("/profile")
async def get_org_profile(
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Return the business profile and config for the user's organisation."""
    from sqlalchemy import select
    from src.infrastructure.database.flowpilot_models import BusinessConfigModel

    business_id = await _get_user_business_id(current_user, session)
    repo = BusinessRepository(session)
    biz = await repo.get_by_id(business_id)
    if biz is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    cfg_result = await session.execute(
        select(BusinessConfigModel).where(BusinessConfigModel.business_id == business_id)
    )
    config = cfg_result.scalar_one_or_none()

    return {
        "id": str(biz.id),
        "business_name": biz.business_name,
        "business_type": biz.business_type,
        "rc_number": biz.rc_number,
        "tax_id": biz.tax_id,
        "city": biz.city,
        "state": biz.state,
        "country": biz.country,
        "website": biz.website,
        "phone": biz.phone,
        "interswitch_merchant_id": biz.interswitch_merchant_id,
        "is_active": biz.is_active,
        "config": {
            "monthly_txn_volume_range": config.monthly_txn_volume_range if config else None,
            "avg_monthly_payouts_range": config.avg_monthly_payouts_range if config else None,
            "primary_bank": config.primary_bank if config else None,
            "primary_use_cases": config.primary_use_cases if config else None,
            "risk_appetite": config.risk_appetite if config else None,
            "default_risk_tolerance": config.default_risk_tolerance if config else None,
            "default_budget_cap": float(config.default_budget_cap) if config and config.default_budget_cap else None,
            "preferences": config.preferences if config else None,
        } if config else None,
    }


@router.patch("/profile")
async def update_org_profile(
    body: UpdateOrgRequest,
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Update the business profile for the user's organisation."""
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided",
        )

    business_id = await _get_user_business_id(current_user, session)
    repo = BusinessRepository(session)
    updated = await repo.update(business_id, **payload)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    return {
        "id": str(updated.id),
        "business_name": updated.business_name,
        "business_type": updated.business_type,
        "rc_number": updated.rc_number,
        "tax_id": updated.tax_id,
        "city": updated.city,
        "state": updated.state,
        "country": updated.country,
        "website": updated.website,
        "phone": updated.phone,
    }


@router.patch("/profile/config")
async def update_org_config(
    body: UpdateOrgConfigRequest,
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Update the business config (financial profile) for the user's organisation."""
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided",
        )

    business_id = await _get_user_business_id(current_user, session)
    repo = BusinessRepository(session)
    config = await repo.update_config(business_id, **payload)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation config not found")

    return {
        "monthly_txn_volume_range": config.monthly_txn_volume_range,
        "avg_monthly_payouts_range": config.avg_monthly_payouts_range,
        "primary_bank": config.primary_bank,
        "primary_use_cases": config.primary_use_cases,
        "risk_appetite": config.risk_appetite,
        "default_risk_tolerance": config.default_risk_tolerance,
        "default_budget_cap": float(config.default_budget_cap) if config.default_budget_cap else None,
    }

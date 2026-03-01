"""
Onboarding routes — create business + config + membership in one step.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.business_repository import (
    BusinessRepository,
)
from src.infrastructure.database.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingRequest(BaseModel):
    business_name: str
    business_type: Optional[str] = None
    monthly_txn_volume_range: Optional[str] = None
    avg_monthly_payouts_range: Optional[str] = None
    primary_bank: Optional[str] = None
    primary_use_cases: Optional[list[str]] = None
    risk_appetite: Optional[str] = None


@router.post("/complete", status_code=status.HTTP_201_CREATED)
async def complete_onboarding(
    body: OnboardingRequest,
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Create a business, its config, and assign the user as owner."""
    # Guard: user should not already have a business
    user_repo = UserRepository(session)
    existing = await user_repo.get_memberships(current_user.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User has already completed onboarding",
        )

    if body.risk_appetite and body.risk_appetite not in (
        "conservative",
        "moderate",
        "aggressive",
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="risk_appetite must be conservative, moderate, or aggressive",
        )

    biz_repo = BusinessRepository(session)
    business, config, member = await biz_repo.create_with_owner(
        owner_id=current_user.id,
        business_name=body.business_name,
        business_type=body.business_type,
        monthly_txn_volume_range=body.monthly_txn_volume_range,
        avg_monthly_payouts_range=body.avg_monthly_payouts_range,
        primary_bank=body.primary_bank,
        primary_use_cases=body.primary_use_cases,
        risk_appetite=body.risk_appetite,
    )

    logger.info("Onboarding complete for user=%s business=%s", current_user.id, business.id)

    return {
        "business": {
            "id": str(business.id),
            "business_name": business.business_name,
            "business_type": business.business_type,
        },
        "config": {
            "onboarding_step": config.onboarding_step,
            "onboarding_completed_at": config.onboarding_completed_at.isoformat(),
            "monthly_txn_volume_range": config.monthly_txn_volume_range,
            "avg_monthly_payouts_range": config.avg_monthly_payouts_range,
            "primary_bank": config.primary_bank,
            "primary_use_cases": config.primary_use_cases,
            "risk_appetite": config.risk_appetite,
        },
        "membership": {
            "business_id": str(member.business_id),
            "user_id": str(member.user_id),
            "role": member.role,
        },
    }

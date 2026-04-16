"""
Onboarding routes — create business + config + membership in one step.
"""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.auth.dependencies import get_current_user
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.business_repository import (
    BusinessRepository,
)
from src.infrastructure.database.repositories.notification_repository import (
    NotificationRepository,
)
from src.infrastructure.database.repositories.user_repository import UserRepository
from src.services.email_service import send_welcome_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingRequest(BaseModel):
    business_name: str
    # "individual" or "business" — determines KYC flow and team visibility
    account_type: str = "business"
    business_type: Optional[str] = None
    # Owner date of birth — must be 18+ at registration
    date_of_birth: Optional[date] = None
    monthly_txn_volume_range: Optional[str] = None
    avg_monthly_payouts_range: Optional[str] = None
    primary_bank: Optional[str] = None
    primary_use_cases: Optional[list[str]] = None
    risk_appetite: Optional[str] = None
    # Step 3 financial setup
    interswitch_merchant_id: Optional[str] = None
    merchant_state: Optional[str] = None
    daily_payout_limit: Optional[float] = None
    single_payout_cap: Optional[float] = None
    risk_alert_threshold: Optional[float] = None
    liquidity_alert_buffer: Optional[float] = None


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

    if body.account_type not in ("individual", "business"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="account_type must be 'individual' or 'business'",
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

    # Validate date_of_birth — required for individual accounts, must be 18+
    if body.account_type == "individual" and not body.date_of_birth:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_of_birth is required for individual accounts.",
        )

    if body.date_of_birth:
        from datetime import date as _date
        today = _date.today()
        age = today.year - body.date_of_birth.year - (
            (today.month, today.day) < (body.date_of_birth.month, body.date_of_birth.day)
        )
        if age < 18:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="You must be at least 18 years old to register.",
            )

        # Persist DOB on the user
        from sqlalchemy import update as sa_update
        from src.infrastructure.database.flowpilot_models import UserModel as _UserModel
        await session.execute(
            sa_update(_UserModel)
            .where(_UserModel.id == current_user.id)
            .values(date_of_birth=body.date_of_birth)
        )

    biz_repo = BusinessRepository(session)
    business, config, member = await biz_repo.create_with_owner(
        owner_id=current_user.id,
        business_name=body.business_name,
        account_type=body.account_type,
        business_type=body.business_type,
        interswitch_merchant_id=body.interswitch_merchant_id,
        monthly_txn_volume_range=body.monthly_txn_volume_range,
        avg_monthly_payouts_range=body.avg_monthly_payouts_range,
        primary_bank=body.primary_bank,
        primary_use_cases=body.primary_use_cases,
        risk_appetite=body.risk_appetite,
        merchant_state=body.merchant_state,
        daily_payout_limit=body.daily_payout_limit,
        single_payout_cap=body.single_payout_cap,
        risk_alert_threshold=body.risk_alert_threshold,
        liquidity_alert_buffer=body.liquidity_alert_buffer,
    )

    logger.info("Onboarding complete for user=%s business=%s", current_user.id, business.id)

    # Create welcome notification
    notif_repo = NotificationRepository(session)
    _is_individual = body.account_type == "individual"
    _welcome_msg = (
        f"Your account is ready. Complete your identity verification to start sending payouts."
        if _is_individual
        else f"Your workspace '{business.business_name}' is ready. Start by creating your first payout run."
    )
    await notif_repo.create(
        user_id=current_user.id,
        business_id=business.id,
        title="Welcome to FlowPilot!",
        message=_welcome_msg,
        type="info",
        resource_type="business",
        resource_id=str(business.id),
    )

    # Notify about virtual account assignment
    if business.virtual_account_number:
        await notif_repo.create(
            user_id=current_user.id,
            business_id=business.id,
            title="Your wallet account details are ready",
            message=(
                f"Fund your FlowPilot wallet by transferring to account "
                f"{business.virtual_account_number} at {business.virtual_account_bank}. "
                "Find these details on your Wallet page."
            ),
            type="success",
            resource_type="business",
            resource_id=str(business.id),
        )

    # Send welcome email — best-effort, never blocks the response
    await send_welcome_email(
        to=current_user.email,
        display_name=current_user.display_name,
        business_name=business.business_name,
        frontend_url=Settings.FRONTEND_URL,
    )

    return {
        "business": {
            "id": str(business.id),
            "business_name": business.business_name,
            "account_type": business.account_type,
            "business_type": business.business_type,
            "virtual_account_number": business.virtual_account_number,
            "virtual_account_bank": business.virtual_account_bank,
            "virtual_account_name": business.virtual_account_name,
        },
        "config": {
            "onboarding_step": config.onboarding_step,
            "onboarding_completed_at": config.onboarding_completed_at.isoformat(),
            "monthly_txn_volume_range": config.monthly_txn_volume_range,
            "avg_monthly_payouts_range": config.avg_monthly_payouts_range,
            "primary_bank": config.primary_bank,
            "primary_use_cases": config.primary_use_cases,
            "risk_appetite": config.risk_appetite,
            "merchant_state": config.merchant_state,
            "daily_payout_limit": float(config.daily_payout_limit) if config.daily_payout_limit else None,
            "single_payout_cap": float(config.single_payout_cap) if config.single_payout_cap else None,
            "risk_alert_threshold": float(config.risk_alert_threshold) if config.risk_alert_threshold else None,
            "liquidity_alert_buffer": float(config.liquidity_alert_buffer) if config.liquidity_alert_buffer else None,
        },
        "membership": {
            "business_id": str(member.business_id),
            "user_id": str(member.user_id),
            "role": member.role,
        },
    }

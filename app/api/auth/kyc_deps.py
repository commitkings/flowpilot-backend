"""
FastAPI dependency that enforces verified KYC for any route that touches
financial data (payouts, wallet, AI credits, scheduled runs).

Usage:
    from app.api.auth.kyc_deps import require_verified_kyc

    @router.get("/runs")
    async def list_runs(
        ...,
        _=Depends(require_verified_kyc),
    ):
        ...

The dependency raises HTTP 403 with an X-KYC-Status header so the frontend
can redirect the user to the correct KYC page without parsing the message text.
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import BusinessMemberModel, BusinessModel


async def require_verified_kyc(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> BusinessModel:
    """Raise 403 if the caller's business has not completed KYC verification.

    Returns the BusinessModel so callers that need it can capture the dep value:
        biz: BusinessModel = Depends(require_verified_kyc)
    """
    mem_result = await session.execute(
        select(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    membership = mem_result.scalars().first()
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active business membership found.",
        )

    biz_result = await session.execute(
        select(BusinessModel).where(BusinessModel.id == membership.business_id)
    )
    biz = biz_result.scalar_one_or_none()

    kyc_status = biz.kyc_status if biz else "not_submitted"

    if kyc_status == "verified":
        return biz  # type: ignore[return-value]

    if kyc_status == "pending":
        detail = (
            "Your business verification is under review. "
            "This feature will be available once approved — typically within 24 hours."
        )
    elif kyc_status == "rejected":
        detail = (
            "Your business verification was rejected. "
            "Resubmit your documents to regain access."
        )
    else:
        detail = (
            "Business verification (KYC) is required to access this feature. "
            "Complete your KYC to continue."
        )

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
        headers={"X-KYC-Status": kyc_status},
    )

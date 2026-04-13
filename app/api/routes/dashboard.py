"""
Dashboard stats endpoint — aggregated metrics for the business overview page.
"""

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func, and_

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    AgentRunModel,
    BusinessMemberModel,
    PayoutCandidateModel,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_ACTIVE_STATUSES = {"planning", "reconciling", "scoring", "forecasting", "executing"}
_LIVE_STATUSES = {"planning", "reconciling", "scoring", "executing"}


async def _get_business_id(current_user, session):
    result = await session.execute(
        select(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id
        )
    )
    membership = result.scalars().first()
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No business membership found",
        )
    return membership.business_id


@router.get("/stats")
async def get_dashboard_stats(
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """
    Return aggregated metrics for the authenticated user's business.

    Includes:
    - total_volume_disbursed  — sum of successfully executed payout amounts
    - runs_this_month         — run count since the start of the current UTC month
    - pending_approvals       — runs currently awaiting approval
    - active_runs             — runs currently processing
    - recent_runs             — last 8 runs (newest first)
    """
    business_id = await _get_business_id(current_user, session)

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # ── Total volume disbursed (all time, successfully executed candidates) ──
    vol_result = await session.execute(
        select(func.coalesce(func.sum(PayoutCandidateModel.amount), 0)).where(
            and_(
                PayoutCandidateModel.business_id == business_id,
                PayoutCandidateModel.execution_status == "success",
            )
        )
    )
    total_volume = float(vol_result.scalar() or 0)

    # ── Runs this month ──
    month_result = await session.execute(
        select(func.count(AgentRunModel.id)).where(
            and_(
                AgentRunModel.business_id == business_id,
                AgentRunModel.created_at >= month_start,
            )
        )
    )
    runs_this_month = int(month_result.scalar() or 0)

    # ── Pending approvals ──
    approval_result = await session.execute(
        select(func.count(AgentRunModel.id)).where(
            and_(
                AgentRunModel.business_id == business_id,
                AgentRunModel.status == "awaiting_approval",
            )
        )
    )
    pending_approvals = int(approval_result.scalar() or 0)

    # ── Active runs ──
    active_result = await session.execute(
        select(func.count(AgentRunModel.id)).where(
            and_(
                AgentRunModel.business_id == business_id,
                AgentRunModel.status.in_(_LIVE_STATUSES),
            )
        )
    )
    active_runs = int(active_result.scalar() or 0)

    # ── Recent runs (last 8, newest first) ──
    recent_result = await session.execute(
        select(AgentRunModel)
        .where(AgentRunModel.business_id == business_id)
        .order_by(AgentRunModel.created_at.desc())
        .limit(8)
    )
    recent_runs_orm = recent_result.scalars().all()
    recent_runs = [
        {
            "run_id": str(r.id),
            "objective": r.objective,
            "status": r.status,
            "candidate_count": r.candidate_count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in recent_runs_orm
    ]

    return {
        "total_volume_disbursed": total_volume,
        "runs_this_month": runs_this_month,
        "pending_approvals": pending_approvals,
        "active_runs": active_runs,
        "recent_runs": recent_runs,
    }

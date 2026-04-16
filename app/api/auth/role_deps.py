"""
FastAPI dependency factory for role-based access control.

Usage:
    from app.api.auth.role_deps import require_role

    @router.post("/runs/{run_id}/approve")
    async def approve(
        ...,
        current_user=Depends(get_current_user),
        _=Depends(require_role("owner", "approver")),
    ):
        ...
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import BusinessMemberModel


def require_role(*allowed_roles: str):
    """Return a FastAPI dependency that enforces the caller has one of *allowed_roles*.

    FastAPI deduplicates get_current_user and get_db_session within the same
    request, so adding this dependency incurs no extra DB round-trips.
    """

    async def _check(
        current_user=Depends(get_current_user),
        session: AsyncSession = Depends(get_db_session),
    ) -> BusinessMemberModel:
        result = await session.execute(
            select(BusinessMemberModel).where(
                BusinessMemberModel.user_id == current_user.id,
                BusinessMemberModel.is_active.is_(True),
            )
        )
        membership = result.scalars().first()
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No business membership found",
            )
        if membership.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Allowed roles: {', '.join(sorted(allowed_roles))}",
            )
        return membership

    return _check

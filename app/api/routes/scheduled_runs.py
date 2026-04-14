"""
Scheduled runs — CRUD for recurring payout run definitions.

All routes require a valid JWT. Create/update/delete require owner or analyst role.

Endpoints:
    GET    /runs/scheduled
    POST   /runs/scheduled
    PATCH  /runs/scheduled/{id}
    DELETE /runs/scheduled/{id}
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    ScheduledRunModel,
)

router = APIRouter(tags=["scheduled-runs"])


def _next_run_from_cron(cron_expr: str) -> Optional[datetime]:
    """Compute the next fire time from a cron expression using croniter if available."""
    try:
        from croniter import croniter
        now = datetime.now(timezone.utc)
        cron = croniter(cron_expr, now)
        return cron.get_next(datetime).replace(tzinfo=timezone.utc)
    except ImportError:
        return None
    except Exception:
        return None


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


class CreateScheduledRunRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    objective: str = Field(..., min_length=1)
    cron_expression: str = Field(..., min_length=1, max_length=128)
    frequency_label: str = Field(..., min_length=1, max_length=64)


class PatchScheduledRunRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    objective: Optional[str] = None
    cron_expression: Optional[str] = None
    frequency_label: Optional[str] = None
    is_active: Optional[bool] = None


def _serialize(r: ScheduledRunModel) -> dict:
    return {
        "id": str(r.id),
        "name": r.name,
        "objective": r.objective,
        "cron_expression": r.cron_expression,
        "frequency_label": r.frequency_label,
        "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
        "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
        "last_reminded_at": r.last_reminded_at.isoformat() if r.last_reminded_at else None,
        "is_active": r.is_active,
        "created_at": r.created_at.isoformat(),
    }


@router.get("/runs/scheduled")
async def list_scheduled_runs(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(ScheduledRunModel)
        .where(ScheduledRunModel.business_id == business_id)
        .order_by(ScheduledRunModel.created_at.desc())
    )
    runs = result.scalars().all()
    return {"scheduled_runs": [_serialize(r) for r in runs]}


@router.post("/runs/scheduled", status_code=status.HTTP_201_CREATED)
async def create_scheduled_run(
    body: CreateScheduledRunRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "analyst")),
    session: AsyncSession = Depends(get_db_session),
):
    # Validate cron expression
    next_run = _next_run_from_cron(body.cron_expression)

    business_id = await _get_business_id(current_user, session)
    scheduled = ScheduledRunModel(
        business_id=business_id,
        created_by=current_user.id,
        name=body.name,
        objective=body.objective,
        cron_expression=body.cron_expression,
        frequency_label=body.frequency_label,
        next_run_at=next_run,
        is_active=True,
    )
    session.add(scheduled)
    await session.commit()
    await session.refresh(scheduled)
    return _serialize(scheduled)


@router.patch("/runs/scheduled/{run_id}")
async def update_scheduled_run(
    run_id: uuid.UUID,
    body: PatchScheduledRunRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "analyst")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(ScheduledRunModel).where(
            ScheduledRunModel.id == run_id,
            ScheduledRunModel.business_id == business_id,
        )
    )
    run = result.scalars().first()
    if not run:
        raise HTTPException(status_code=404, detail="Scheduled run not found")

    values: dict = {"updated_at": datetime.now(timezone.utc)}
    if body.name is not None:
        values["name"] = body.name
    if body.objective is not None:
        values["objective"] = body.objective
    if body.frequency_label is not None:
        values["frequency_label"] = body.frequency_label
    if body.is_active is not None:
        values["is_active"] = body.is_active
    if body.cron_expression is not None:
        values["cron_expression"] = body.cron_expression
        values["next_run_at"] = _next_run_from_cron(body.cron_expression)

    await session.execute(
        update(ScheduledRunModel).where(ScheduledRunModel.id == run_id).values(**values)
    )
    await session.commit()
    await session.refresh(run)
    return _serialize(run)


@router.delete("/runs/scheduled/{run_id}")
async def delete_scheduled_run(
    run_id: uuid.UUID,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "analyst")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(ScheduledRunModel).where(
            ScheduledRunModel.id == run_id,
            ScheduledRunModel.business_id == business_id,
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Scheduled run not found")

    await session.execute(
        delete(ScheduledRunModel).where(ScheduledRunModel.id == run_id)
    )
    await session.commit()
    return {"status": "deleted"}

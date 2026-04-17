"""
Scheduled runs — CRUD for recurring and one-time payout run definitions.

All routes require a valid JWT. Create/update/delete require owner or analyst role.

Endpoints:
    GET    /runs/scheduled
    POST   /runs/scheduled
    PATCH  /runs/scheduled/{id}
    DELETE /runs/scheduled/{id}
"""

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.auth.kyc_deps import require_verified_kyc
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    BusinessModel,
    ScheduledRunModel,
    UserModel,
)
from src.infrastructure.database.repositories.notification_repository import NotificationRepository

import logging
logger = logging.getLogger(__name__)

router = APIRouter(tags=["scheduled-runs"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _next_run_from_cron(cron_expr: Optional[str]) -> Optional[datetime]:
    """Compute the next fire time from a cron expression using croniter if available."""
    if not cron_expr or not cron_expr.strip():
        return None
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


# ── Request / Response schemas ────────────────────────────────────────────────

class CreateScheduledRunRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    objective: str = Field(..., min_length=1)
    run_type: Literal["recurring", "one_time"] = "recurring"

    # Required for recurring; omit for one_time
    cron_expression: Optional[str] = Field(None, max_length=128)
    frequency_label: str = Field(..., min_length=1, max_length=64)

    # Required for one_time — ISO 8601 UTC datetime for the single execution
    run_at: Optional[datetime] = None

    # Optional payout configuration — stored so the edit form can pre-populate
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    risk_tolerance: Optional[float] = None
    budget_cap: Optional[float] = None
    assigned_approver_id: Optional[str] = None
    candidates: Optional[list] = None

    @model_validator(mode="after")
    def _validate_by_type(self) -> "CreateScheduledRunRequest":
        if self.run_type == "recurring":
            if not self.cron_expression or not self.cron_expression.strip():
                raise ValueError("cron_expression is required for recurring runs.")
        else:  # one_time
            if self.run_at is None:
                raise ValueError("run_at is required for one-time runs.")
            # Ensure run_at is timezone-aware
            run_at = self.run_at
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
            if run_at <= datetime.now(timezone.utc):
                raise ValueError("run_at must be a future date and time.")
            self.run_at = run_at
        return self


class PatchScheduledRunRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    objective: Optional[str] = None
    cron_expression: Optional[str] = None
    frequency_label: Optional[str] = None
    is_active: Optional[bool] = None
    # For one-time runs: reschedule to a new future datetime
    run_at: Optional[datetime] = None
    # Payout config update
    run_config: Optional[dict] = None


def _serialize(r: ScheduledRunModel) -> dict:
    return {
        "id": str(r.id),
        "name": r.name,
        "objective": r.objective,
        "run_type": r.run_type,
        "cron_expression": r.cron_expression,
        "frequency_label": r.frequency_label,
        "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
        "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
        "last_reminded_at": r.last_reminded_at.isoformat() if r.last_reminded_at else None,
        "is_active": r.is_active,
        "run_config": r.run_config,
        "created_at": r.created_at.isoformat(),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

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


@router.get("/runs/scheduled/{run_id}")
async def get_scheduled_run(
    run_id: uuid.UUID,
    current_user=Depends(get_current_user),
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
    return _serialize(run)


@router.post("/runs/scheduled", status_code=status.HTTP_201_CREATED)
async def create_scheduled_run(
    body: CreateScheduledRunRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "analyst")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)

    # KYC gate
    biz_result = await session.execute(
        select(BusinessModel).where(BusinessModel.id == business_id)
    )
    biz = biz_result.scalar_one_or_none()
    if biz and biz.kyc_status != "verified":
        detail = (
            "Your business is pending KYC verification. Scheduled runs will be available once verified."
            if biz.kyc_status == "pending"
            else "Business verification (KYC) is required before creating scheduled runs. Please complete your KYC."
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

    # Resolve timing
    if body.run_type == "recurring":
        next_run = _next_run_from_cron(body.cron_expression)
        cron_expression = body.cron_expression
    else:
        # One-time: use run_at directly (already validated as future)
        next_run = body.run_at
        cron_expression = None

    run_config: dict = {}
    if body.date_from: run_config["date_from"] = body.date_from
    if body.date_to: run_config["date_to"] = body.date_to
    if body.risk_tolerance is not None: run_config["risk_tolerance"] = body.risk_tolerance
    if body.budget_cap is not None: run_config["budget_cap"] = body.budget_cap
    if body.assigned_approver_id: run_config["assigned_approver_id"] = body.assigned_approver_id
    if body.candidates: run_config["candidates"] = body.candidates

    scheduled = ScheduledRunModel(
        business_id=business_id,
        created_by=current_user.id,
        name=body.name,
        objective=body.objective,
        run_type=body.run_type,
        cron_expression=cron_expression,
        frequency_label=body.frequency_label,
        next_run_at=next_run,
        is_active=True,
        run_config=run_config or None,
    )
    session.add(scheduled)
    await session.commit()
    await session.refresh(scheduled)

    # Notify owner — best-effort
    try:
        owner_result = await session.execute(
            select(BusinessMemberModel, UserModel)
            .join(UserModel, BusinessMemberModel.user_id == UserModel.id)
            .where(
                BusinessMemberModel.business_id == business_id,
                BusinessMemberModel.role == "owner",
                BusinessMemberModel.is_active.is_(True),
            )
            .limit(1)
        )
        owner_row = owner_result.first()
        if owner_row:
            _, owner_user = owner_row
            next_run_str = (
                scheduled.next_run_at.strftime("%A, %d %B %Y at %I:%M %p UTC")
                if scheduled.next_run_at else "—"
            )
            type_label = "one-time payout" if body.run_type == "one_time" else f"recurring payout ({scheduled.frequency_label})"

            notif_repo = NotificationRepository(session)
            await notif_repo.create(
                user_id=owner_user.id,
                business_id=business_id,
                title="Scheduled run created",
                message=(
                    f'"{scheduled.name}" ({type_label}) has been set up. '
                    f"{'Runs on' if body.run_type == 'one_time' else 'First run:'} {next_run_str}."
                ),
                type="success",
                resource_type="scheduled_run",
                resource_id=str(scheduled.id),
            )
            await session.commit()

            import asyncio as _asyncio
            from src.services.email_service import send_scheduled_run_created_email
            _asyncio.create_task(
                send_scheduled_run_created_email(
                    to=owner_user.email,
                    display_name=owner_user.display_name or owner_user.email,
                    schedule_name=scheduled.name,
                    objective=scheduled.objective,
                    frequency_label=scheduled.frequency_label,
                    next_run_at=next_run_str,
                )
            )
    except Exception as _exc:
        logger.warning("[ScheduledRun] Could not send creation notification: %s", _exc)

    return _serialize(scheduled)


@router.patch("/runs/scheduled/{run_id}")
async def update_scheduled_run(
    run_id: uuid.UUID,
    body: PatchScheduledRunRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "analyst")),
    _kyc=Depends(require_verified_kyc),
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

    # Recurring: update cron and recompute next_run_at
    if body.cron_expression is not None:
        if run.run_type == "one_time":
            raise HTTPException(
                status_code=400,
                detail="Cannot set a cron expression on a one-time run. Update run_at instead.",
            )
        values["cron_expression"] = body.cron_expression
        values["next_run_at"] = _next_run_from_cron(body.cron_expression)

    if body.run_config is not None:
        values["run_config"] = body.run_config

    # One-time: reschedule to a new future datetime
    if body.run_at is not None:
        run_at = body.run_at
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        if run_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=400,
                detail="run_at must be a future date and time.",
            )
        if run.run_type != "one_time":
            raise HTTPException(
                status_code=400,
                detail="run_at can only be updated on one-time runs.",
            )
        values["next_run_at"] = run_at
        # Re-activate if it had been deactivated / cancelled
        values["is_active"] = True

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
    _kyc=Depends(require_verified_kyc),
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

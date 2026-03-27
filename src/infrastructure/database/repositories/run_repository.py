from __future__ import annotations

import datetime
from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select, update, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.infrastructure.database.flowpilot_models import AgentRunModel


class RunRepository:
    """Manages AgentRun persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        business_id: UUID,
        created_by: UUID,
        objective: str,
        merchant_id: str,
        constraints: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        risk_tolerance: Decimal = Decimal("0.3500"),
        budget_cap: Decimal | None = None,
    ) -> AgentRunModel:
        run = AgentRunModel(
            business_id=business_id,
            created_by=created_by,
            objective=objective,
            merchant_id=merchant_id,
            constraints=constraints,
            date_from=date_from,
            date_to=date_to,
            risk_tolerance=risk_tolerance,
            budget_cap=budget_cap,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get_by_id(self, run_id: UUID) -> AgentRunModel | None:
        stmt = (
            select(AgentRunModel)
            .options(
                selectinload(AgentRunModel.run_steps),
                selectinload(AgentRunModel.reconciled_transactions),
                selectinload(AgentRunModel.payout_candidates),
                selectinload(AgentRunModel.payout_batches),
            )
            .where(AgentRunModel.id == run_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Filter helpers ───────────────────────────────────────

    def _apply_filters(
        self,
        stmt,
        *,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ):
        R = AgentRunModel
        filters = []
        if status is not None:
            filters.append(R.status == status)
        if search is not None:
            like = f"%{search}%"
            filters.append(or_(R.objective.ilike(like), R.constraints.ilike(like)))
        if from_date is not None:
            filters.append(R.created_at >= datetime.datetime.combine(from_date, datetime.time.min))
        if to_date is not None:
            filters.append(R.created_at <= datetime.datetime.combine(to_date, datetime.time.max))
        if filters:
            stmt = stmt.where(and_(*filters))
        return stmt

    async def list_by_business(
        self,
        business_id: UUID,
        limit: int = 50,
        offset: int = 0,
        *,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> tuple[list[AgentRunModel], int]:
        R = AgentRunModel
        base = select(R).where(R.business_id == business_id).order_by(R.created_at.desc())
        base = self._apply_filters(base, status=status, search=search, from_date=from_date, to_date=to_date)
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_stmt)).scalar_one()
        rows_stmt = base.limit(limit).offset(offset)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> tuple[list[AgentRunModel], int]:
        R = AgentRunModel
        base = select(R).order_by(R.created_at.desc())
        base = self._apply_filters(base, status=status, search=search, from_date=from_date, to_date=to_date)
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_stmt)).scalar_one()
        rows_stmt = base.limit(limit).offset(offset)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def update_status(
        self, run_id: UUID, status: str, error_message: str | None = None
    ) -> AgentRunModel:
        stmt = (
            update(AgentRunModel)
            .where(AgentRunModel.id == run_id)
            .values(status=status, error_message=error_message)
            .returning(AgentRunModel)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one()

    async def update_plan_graph(self, run_id: UUID, plan_graph: dict) -> None:
        stmt = (
            update(AgentRunModel)
            .where(AgentRunModel.id == run_id)
            .values(plan_graph=plan_graph)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def mark_started(self, run_id: UUID) -> None:
        stmt = (
            update(AgentRunModel)
            .where(AgentRunModel.id == run_id)
            .values(started_at=func.now())
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def mark_completed(self, run_id: UUID, status: str = "completed") -> None:
        stmt = (
            update(AgentRunModel)
            .where(AgentRunModel.id == run_id)
            .values(completed_at=func.now(), status=status)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def transition_status(
        self, run_id: UUID, from_status: str, to_status: str
    ) -> bool:
        """Atomically transition status only if current status matches from_status.

        Returns True if the transition succeeded (exactly one row updated).
        Use this to prevent race conditions on approval/execution gates.
        """
        stmt = (
            update(AgentRunModel)
            .where(
                AgentRunModel.id == run_id,
                AgentRunModel.status == from_status,
            )
            .values(status=to_status)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

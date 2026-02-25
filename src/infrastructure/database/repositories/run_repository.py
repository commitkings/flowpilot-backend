from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, update, func
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
        risk_tolerance: Decimal = Decimal("0.3500"),
        budget_cap: Decimal | None = None,
    ) -> AgentRunModel:
        run = AgentRunModel(
            business_id=business_id,
            created_by=created_by,
            objective=objective,
            merchant_id=merchant_id,
            constraints=constraints,
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

    async def list_by_business(
        self, business_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[AgentRunModel]:
        stmt = (
            select(AgentRunModel)
            .where(AgentRunModel.business_id == business_id)
            .order_by(AgentRunModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_all(
        self, limit: int = 50, offset: int = 0
    ) -> list[AgentRunModel]:
        stmt = (
            select(AgentRunModel)
            .order_by(AgentRunModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

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

    async def mark_completed(self, run_id: UUID) -> None:
        stmt = (
            update(AgentRunModel)
            .where(AgentRunModel.id == run_id)
            .values(completed_at=func.now(), status="completed")
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

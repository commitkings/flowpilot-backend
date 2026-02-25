from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import RunStepModel


class PlanStepRepository:
    """Manages RunStep persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_batch(
        self, run_id: UUID, steps: list[dict]
    ) -> list[RunStepModel]:
        models = [
            RunStepModel(run_id=run_id, **step) for step in steps
        ]
        self._session.add_all(models)
        await self._session.flush()
        return models

    async def get_by_run(self, run_id: UUID) -> list[RunStepModel]:
        stmt = (
            select(RunStepModel)
            .where(RunStepModel.run_id == run_id)
            .order_by(RunStepModel.step_order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        step_id: UUID,
        status: str,
        output_data: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        values: dict = {"status": status}
        if output_data is not None:
            values["output_data"] = output_data
        if error_message is not None:
            values["error_message"] = error_message
        stmt = (
            update(RunStepModel)
            .where(RunStepModel.id == step_id)
            .values(**values)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def mark_started(self, step_id: UUID) -> None:
        stmt = (
            update(RunStepModel)
            .where(RunStepModel.id == step_id)
            .values(status="running", started_at=func.now())
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def mark_completed(
        self, step_id: UUID, output_data: dict | None = None
    ) -> None:
        values: dict = {"status": "completed", "completed_at": func.now()}
        if output_data is not None:
            values["output_data"] = output_data
        stmt = (
            update(RunStepModel)
            .where(RunStepModel.id == step_id)
            .values(**values)
        )
        await self._session.execute(stmt)
        await self._session.flush()

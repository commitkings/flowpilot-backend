from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import RunEventModel


class RunEventRepository:
    """Manages RunEvent persistence and retrieval for SSE streaming."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        run_id: UUID,
        event_type: str,
        payload: dict,
        sequence_num: int,
        step_id: Optional[UUID] = None,
    ) -> RunEventModel:
        event = RunEventModel(
            run_id=run_id,
            step_id=step_id,
            event_type=event_type,
            payload=payload,
            sequence_num=sequence_num,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def get_events_since(
        self, run_id: UUID, last_sequence_num: int = 0
    ) -> list[RunEventModel]:
        """Get events after a given sequence number (for SSE replay/reconnect)."""
        stmt = (
            select(RunEventModel)
            .where(
                RunEventModel.run_id == run_id,
                RunEventModel.sequence_num > last_sequence_num,
            )
            .order_by(RunEventModel.sequence_num.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_all_events(self, run_id: UUID) -> list[RunEventModel]:
        """Get all events for a run (for completed run replay)."""
        return await self.get_events_since(run_id, last_sequence_num=0)

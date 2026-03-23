from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import AuditLogModel


class AuditRepository:
    """Manages AuditLog persistence and retrieval (append-only)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        run_id: UUID,
        action: str,
        agent_type: str | None = None,
        step_id: UUID | None = None,
        detail: dict | None = None,
        api_endpoint: str | None = None,
        request_hash: str | None = None,
        response_status: int | None = None,
        response_time_ms: int | None = None,
    ) -> AuditLogModel:
        entry = AuditLogModel(
            run_id=run_id,
            action=action,
            agent_type=agent_type,
            step_id=step_id,
            detail=detail,
            api_endpoint=api_endpoint,
            request_hash=request_hash,
            response_status=response_status,
            response_time_ms=response_time_ms,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def append_batch(self, entries: list[dict]) -> list[AuditLogModel]:
        models = [AuditLogModel(**entry) for entry in entries]
        self._session.add_all(models)
        await self._session.flush()
        return models

    async def get_by_run(self, run_id: UUID) -> list[AuditLogModel]:
        stmt = (
            select(AuditLogModel)
            .where(AuditLogModel.run_id == run_id)
            .order_by(AuditLogModel.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_run(self, run_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(AuditLogModel)
            .where(AuditLogModel.run_id == run_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

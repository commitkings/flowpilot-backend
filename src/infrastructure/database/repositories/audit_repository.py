from __future__ import annotations

import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import select, func, and_
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
    ) -> AuditLogModel:
        entry = AuditLogModel(
            run_id=run_id,
            action=action,
            agent_type=agent_type,
            step_id=step_id,
            detail=detail,
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

    # ── Global query helpers (system-wide audit trail) ────────

    def _apply_filters(
        self,
        stmt,
        *,
        run_id: Optional[UUID] = None,
        agent_type: Optional[str] = None,
        action: Optional[str] = None,
        from_date: Optional[datetime.date] = None,
        to_date: Optional[datetime.date] = None,
    ):
        A = AuditLogModel
        filters = []
        if run_id is not None:
            filters.append(A.run_id == run_id)
        if agent_type is not None:
            filters.append(A.agent_type == agent_type)
        if action is not None:
            filters.append(A.action == action)
        if from_date is not None:
            filters.append(A.created_at >= datetime.datetime.combine(from_date, datetime.time.min))
        if to_date is not None:
            filters.append(A.created_at <= datetime.datetime.combine(to_date, datetime.time.max))
        if filters:
            stmt = stmt.where(and_(*filters))
        return stmt

    async def list_all(
        self,
        *,
        run_id: Optional[UUID] = None,
        agent_type: Optional[str] = None,
        action: Optional[str] = None,
        from_date: Optional[datetime.date] = None,
        to_date: Optional[datetime.date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AuditLogModel], int]:
        """Return (rows, total_count) for the global audit trail."""
        A = AuditLogModel
        base = select(A).order_by(A.created_at.desc())
        base = self._apply_filters(
            base, run_id=run_id, agent_type=agent_type, action=action,
            from_date=from_date, to_date=to_date,
        )
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_stmt)).scalar_one()
        rows_stmt = base.limit(limit).offset(offset)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

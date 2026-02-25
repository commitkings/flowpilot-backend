from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import InstitutionModel


class InstitutionRepository:
    """Manages Institution persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_batch(self, institutions: list[dict]) -> int:
        if not institutions:
            return 0
        stmt = insert(InstitutionModel).values(institutions)
        stmt = stmt.on_conflict_do_update(
            index_elements=["institution_code"],
            set_={
                "institution_name": stmt.excluded.institution_name,
                "short_name": stmt.excluded.short_name,
                "institution_type": stmt.excluded.institution_type,
                "is_active": stmt.excluded.is_active,
                "last_synced_at": stmt.excluded.last_synced_at,
                "raw_response": stmt.excluded.raw_response,
            },
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount

    async def get_all_active(self) -> list[InstitutionModel]:
        stmt = select(InstitutionModel).where(
            InstitutionModel.is_active.is_(True)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_code(self, code: str) -> InstitutionModel | None:
        stmt = select(InstitutionModel).where(
            InstitutionModel.institution_code == code
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

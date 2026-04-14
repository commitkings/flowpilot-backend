from __future__ import annotations

from typing import Optional

from sqlalchemy import func, or_, select
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
                "nip_code": stmt.excluded.nip_code,
                "cbn_code": stmt.excluded.cbn_code,
                "institution_type": stmt.excluded.institution_type,
                "is_active": stmt.excluded.is_active,
                "last_synced_at": stmt.excluded.last_synced_at,
                "raw_response": stmt.excluded.raw_response,
            },
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount

    async def get_all_active(
        self,
        search: Optional[str] = None,
        institution_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[InstitutionModel], int]:
        filters = [InstitutionModel.is_active.is_(True)]

        if search:
            term = f"%{search.lower()}%"
            filters.append(
                or_(
                    func.lower(InstitutionModel.institution_name).like(term),
                    func.lower(InstitutionModel.short_name).like(term),
                    InstitutionModel.institution_code.like(term),
                )
            )

        if institution_type:
            filters.append(InstitutionModel.institution_type == institution_type)

        base = select(InstitutionModel).where(*filters)

        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_stmt)).scalar() or 0

        rows_stmt = (
            base.order_by(InstitutionModel.institution_name)
            .limit(limit)
            .offset(offset)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def get_all_active_rows(
        self,
        search: Optional[str] = None,
        institution_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[InstitutionModel]:
        """Convenience wrapper for callers that only need institution rows.

        This avoids tuple/list mixups at call sites that do alias resolution and
        never use the total count.
        """
        rows, _total = await self.get_all_active(
            search=search,
            institution_type=institution_type,
            limit=limit,
            offset=offset,
        )
        return rows

    async def get_by_code(self, code: str) -> InstitutionModel | None:
        stmt = select(InstitutionModel).where(
            InstitutionModel.institution_code == code
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

"""Long-term run digests + similarity search (pg_trgm)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import RunMemoryDigestModel


class RunMemoryDigestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_for_run(
        self,
        run_id: UUID,
        business_id: UUID,
        objective: str,
        digest_summary: str,
        candidate_count: int,
        blocked_count: int,
        failed_count: int,
    ) -> None:
        await self._session.execute(
            delete(RunMemoryDigestModel).where(RunMemoryDigestModel.run_id == run_id)
        )
        row = RunMemoryDigestModel(
            run_id=run_id,
            business_id=business_id,
            objective=objective[:4000],
            digest_summary=digest_summary[:8000],
            candidate_count=candidate_count,
            blocked_count=blocked_count,
            failed_count=failed_count,
        )
        self._session.add(row)
        await self._session.flush()

    async def search_similar(
        self,
        business_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if len(q) < 2:
            return []

        stmt = text(
            """
            SELECT run_id::text AS run_id,
                   objective,
                   digest_summary,
                   candidate_count,
                   blocked_count,
                   failed_count,
                   GREATEST(
                       similarity(objective, :q),
                       similarity(digest_summary, :q)
                   ) AS score
            FROM run_memory_digest
            WHERE business_id = :bid
            ORDER BY score DESC
            LIMIT :lim
            """
        )
        result = await self._session.execute(
            stmt,
            {"q": q, "bid": str(business_id), "lim": limit},
        )
        rows = result.mappings().all()
        return [dict(r) for r in rows]

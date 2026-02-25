from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import ReconciledTransactionModel


class TransactionRepository:
    """Manages ReconciledTransaction persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_batch(
        self, run_id: UUID, business_id: UUID, transactions: list[dict]
    ) -> list[ReconciledTransactionModel]:
        if not transactions:
            return []
        rows = [{**txn, "run_id": run_id, "business_id": business_id} for txn in transactions]
        stmt = (
            insert(ReconciledTransactionModel)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["run_id", "interswitch_ref"]
            )
            .returning(ReconciledTransactionModel)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return list(result.scalars().all())

    async def get_by_run(self, run_id: UUID) -> list[ReconciledTransactionModel]:
        stmt = select(ReconciledTransactionModel).where(ReconciledTransactionModel.run_id == run_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_run(self, run_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(ReconciledTransactionModel)
            .where(ReconciledTransactionModel.run_id == run_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def get_anomalies(self, run_id: UUID) -> list[ReconciledTransactionModel]:
        stmt = (
            select(ReconciledTransactionModel)
            .where(
                ReconciledTransactionModel.run_id == run_id,
                ReconciledTransactionModel.has_anomaly.is_(True),
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

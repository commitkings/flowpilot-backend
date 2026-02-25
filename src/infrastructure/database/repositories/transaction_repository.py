from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import TransactionModel


class TransactionRepository:
    """Manages Transaction persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_batch(
        self, run_id: UUID, transactions: list[dict]
    ) -> list[TransactionModel]:
        if not transactions:
            return []
        rows = [{**txn, "run_id": run_id} for txn in transactions]
        stmt = (
            insert(TransactionModel)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["run_id", "transaction_reference"]
            )
            .returning(TransactionModel)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return list(result.scalars().all())

    async def get_by_run(self, run_id: UUID) -> list[TransactionModel]:
        stmt = select(TransactionModel).where(TransactionModel.run_id == run_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_run(self, run_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(TransactionModel)
            .where(TransactionModel.run_id == run_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def get_anomalies(self, run_id: UUID) -> list[TransactionModel]:
        stmt = (
            select(TransactionModel)
            .where(
                TransactionModel.run_id == run_id,
                TransactionModel.is_anomaly.is_(True),
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_resolved(
        self, run_id: UUID, resolved: list[dict]
    ) -> int:
        """Update transactions resolved by reference_search.

        Each dict in resolved must have 'transaction_reference' and 'new_status',
        and may include 'processor_response_code', 'processor_response_message',
        'settlement_date'.
        """
        updated = 0
        for r in resolved:
            ref = r.get("transaction_reference")
            if not ref:
                continue
            values: dict = {"status": r["new_status"]}
            if r.get("processor_response_code"):
                values["processor_response_code"] = r["processor_response_code"]
            if r.get("processor_response_message"):
                values["processor_response_message"] = r["processor_response_message"]
            if r.get("settlement_date"):
                values["settlement_date"] = r["settlement_date"]
            stmt = (
                update(TransactionModel)
                .where(
                    TransactionModel.run_id == run_id,
                    TransactionModel.transaction_reference == ref,
                )
                .values(**values)
            )
            result = await self._session.execute(stmt)
            updated += result.rowcount
        if updated:
            await self._session.flush()
        return updated

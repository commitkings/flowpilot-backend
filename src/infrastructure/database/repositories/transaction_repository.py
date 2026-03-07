from __future__ import annotations

import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import select, update, func, and_, cast, String
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

    # ── Query helpers for the transactions endpoint ──────────

    def _apply_filters(
        self,
        stmt,
        *,
        run_id: Optional[UUID] = None,
        business_id: Optional[UUID] = None,
        status: Optional[str] = None,
        channel: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[datetime.date] = None,
        to_date: Optional[datetime.date] = None,
    ):
        T = ReconciledTransactionModel
        filters = []
        if run_id is not None:
            filters.append(T.run_id == run_id)
        if business_id is not None:
            filters.append(T.business_id == business_id)
        if status is not None:
            filters.append(T.status == status.upper())
        if channel is not None:
            filters.append(T.channel == channel.upper())
        if search is not None:
            filters.append(T.interswitch_ref.ilike(f"%{search}%"))
        if from_date is not None:
            filters.append(T.transaction_timestamp >= datetime.datetime.combine(from_date, datetime.time.min))
        if to_date is not None:
            filters.append(T.transaction_timestamp <= datetime.datetime.combine(to_date, datetime.time.max))
        if filters:
            stmt = stmt.where(and_(*filters))
        return stmt

    async def list_all(
        self,
        *,
        run_id: Optional[UUID] = None,
        business_id: Optional[UUID] = None,
        status: Optional[str] = None,
        channel: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[datetime.date] = None,
        to_date: Optional[datetime.date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ReconciledTransactionModel], int]:
        """Return (rows, total_count) applying filters + pagination."""
        T = ReconciledTransactionModel

        base = select(T).order_by(T.transaction_timestamp.desc())
        base = self._apply_filters(
            base, run_id=run_id, business_id=business_id, status=status, channel=channel,
            search=search, from_date=from_date, to_date=to_date,
        )
        # total count
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_stmt)).scalar_one()
        # paginated rows
        rows_stmt = base.limit(limit).offset(offset)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def get_summary(
        self,
        *,
        run_id: Optional[UUID] = None,
        business_id: Optional[UUID] = None,
        status: Optional[str] = None,
        channel: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[datetime.date] = None,
        to_date: Optional[datetime.date] = None,
    ) -> dict:
        """Aggregate metrics for the metric cards."""
        T = ReconciledTransactionModel
        base = select(
            func.count().label("total_transactions"),
            func.coalesce(func.sum(T.amount), 0).label("total_volume"),
            func.count().filter(T.has_anomaly.is_(True)).label("anomaly_count"),
            func.count().filter(T.status == "FAILED").label("failed_count"),
        )
        base = self._apply_filters(
            base, run_id=run_id, business_id=business_id, status=status, channel=channel,
            search=search, from_date=from_date, to_date=to_date,
        )
        row = (await self._session.execute(base)).one()
        return {
            "total_transactions": row.total_transactions,
            "total_volume": float(row.total_volume),
            "anomaly_count": row.anomaly_count,
            "failed_count": row.failed_count,
        }

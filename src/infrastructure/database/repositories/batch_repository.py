from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from uuid import UUID

from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import PayoutBatchModel


class BatchRepository:
    """Manages PayoutBatch persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        run_id: UUID,
        business_id: UUID,
        batch_reference: str,
        currency: str,
        source_account_id: str,
        total_amount: Decimal,
        item_count: int,
        submission_status: str = "pending",
        accepted_count: int = 0,
        rejected_count: int = 0,
    ) -> PayoutBatchModel:
        # Idempotency: return existing batch if batch_reference already used
        existing = await self.get_by_reference(batch_reference)
        if existing is not None:
            return existing

        batch = PayoutBatchModel(
            run_id=run_id,
            business_id=business_id,
            batch_reference=batch_reference,
            currency=currency,
            source_account_id=source_account_id,
            total_amount=total_amount,
            item_count=item_count,
            submission_status=submission_status,
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            submitted_at=datetime.utcnow(),
        )
        self._session.add(batch)
        await self._session.flush()
        return batch

    async def get_by_reference(
        self, batch_reference: str
    ) -> Optional[PayoutBatchModel]:
        stmt = select(PayoutBatchModel).where(
            PayoutBatchModel.batch_reference == batch_reference
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def update_status(
        self,
        batch_id: UUID,
        submission_status: str,
        accepted_count: int | None = None,
        rejected_count: int | None = None,
    ) -> None:
        values: dict = {"submission_status": submission_status}
        if accepted_count is not None:
            values["accepted_count"] = accepted_count
        if rejected_count is not None:
            values["rejected_count"] = rejected_count
        stmt = (
            update(PayoutBatchModel)
            .where(PayoutBatchModel.id == batch_id)
            .values(**values)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def get_by_run(self, run_id: UUID) -> list[PayoutBatchModel]:
        stmt = (
            select(PayoutBatchModel)
            .where(PayoutBatchModel.run_id == run_id)
            .order_by(PayoutBatchModel.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

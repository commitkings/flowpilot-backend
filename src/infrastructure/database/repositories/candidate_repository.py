from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import PayoutCandidateModel


class CandidateRepository:
    """Manages PayoutCandidate persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_batch(
        self, run_id: UUID, candidates: list[dict], business_id: UUID | None = None,
    ) -> list[PayoutCandidateModel]:
        models = [
            PayoutCandidateModel(run_id=run_id, business_id=business_id, **candidate)
            for candidate in candidates
        ]
        self._session.add_all(models)
        await self._session.flush()
        return models

    async def get_by_run(
        self,
        run_id: UUID,
        approval_status: str | None = None,
        risk_decision: str | None = None,
    ) -> list[PayoutCandidateModel]:
        stmt = select(PayoutCandidateModel).where(
            PayoutCandidateModel.run_id == run_id
        )
        if approval_status is not None:
            stmt = stmt.where(
                PayoutCandidateModel.approval_status == approval_status
            )
        if risk_decision is not None:
            stmt = stmt.where(
                PayoutCandidateModel.risk_decision == risk_decision
            )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def approve(self, candidate_ids: list[UUID], approved_by: UUID, run_id: UUID) -> int:
        stmt = (
            update(PayoutCandidateModel)
            .where(
                PayoutCandidateModel.id.in_(candidate_ids),
                PayoutCandidateModel.run_id == run_id,
            )
            .values(
                approval_status="approved",
                approved_by=approved_by,
                approved_at=func.now(),
            )
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount

    async def reject(self, candidate_ids: list[UUID], run_id: UUID) -> int:
        # Only reject candidates still pending — prevents race with concurrent approve
        stmt = (
            update(PayoutCandidateModel)
            .where(
                PayoutCandidateModel.id.in_(candidate_ids),
                PayoutCandidateModel.run_id == run_id,
                PayoutCandidateModel.approval_status == "pending",
            )
            .values(approval_status="rejected")
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount

    async def update_risk_scoring(
        self,
        candidate_id: UUID,
        risk_score: Decimal,
        risk_reasons: list,
        risk_decision: str,
        run_id: UUID | None = None,
    ) -> None:
        conditions = [PayoutCandidateModel.id == candidate_id]
        if run_id is not None:
            conditions.append(PayoutCandidateModel.run_id == run_id)
        stmt = (
            update(PayoutCandidateModel)
            .where(*conditions)
            .values(
                risk_score=risk_score,
                risk_reasons=risk_reasons,
                risk_decision=risk_decision,
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def update_lookup(
        self,
        candidate_id: UUID,
        lookup_status: str,
        lookup_account_name: str | None = None,
        lookup_match_score: Decimal | None = None,
        transaction_reference: str | None = None,
    ) -> None:
        values: dict = {"lookup_status": lookup_status}
        if lookup_account_name is not None:
            values["lookup_account_name"] = lookup_account_name
        if lookup_match_score is not None:
            values["lookup_match_score"] = lookup_match_score
        if transaction_reference is not None:
            values["transaction_reference"] = transaction_reference
        stmt = (
            update(PayoutCandidateModel)
            .where(PayoutCandidateModel.id == candidate_id)
            .values(**values)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def update_execution(
        self,
        candidate_id: UUID,
        execution_status: str,
        client_reference: str | None = None,
        provider_reference: str | None = None,
        transaction_reference: str | None = None,
        batch_id: UUID | None = None,
        executed_at=None,
    ) -> None:
        values: dict = {"execution_status": execution_status}
        if client_reference is not None:
            values["client_reference"] = client_reference
        if provider_reference is not None:
            values["provider_reference"] = provider_reference
        if transaction_reference is not None:
            values["transaction_reference"] = transaction_reference
        if batch_id is not None:
            values["batch_id"] = batch_id
        if executed_at is not None:
            values["executed_at"] = executed_at
        stmt = (
            update(PayoutCandidateModel)
            .where(PayoutCandidateModel.id == candidate_id)
            .values(**values)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def count_by_run(self, run_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(PayoutCandidateModel)
            .where(PayoutCandidateModel.run_id == run_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

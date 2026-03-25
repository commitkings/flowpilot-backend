from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select, update, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import (
    AgentRunModel,
    PayoutCandidateModel,
)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class CandidateRepository:
    """Manages PayoutCandidate persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_batch(
        self,
        run_id: UUID,
        candidates: list[dict],
        business_id: UUID | None = None,
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
        stmt = select(PayoutCandidateModel).where(PayoutCandidateModel.run_id == run_id)
        if approval_status is not None:
            stmt = stmt.where(PayoutCandidateModel.approval_status == approval_status)
        if risk_decision is not None:
            stmt = stmt.where(PayoutCandidateModel.risk_decision == risk_decision)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def approve(
        self, candidate_ids: list[UUID], approved_by: UUID, run_id: UUID
    ) -> int:
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

    # ── Global query helpers (cross-run approvals queue) ──────

    def _apply_filters(
        self,
        stmt,
        *,
        business_id: Optional[UUID] = None,
        run_id: Optional[UUID] = None,
        approval_status: Optional[str] = None,
        risk_decision: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[datetime.date] = None,
        to_date: Optional[datetime.date] = None,
    ):
        C = PayoutCandidateModel
        filters = []
        if business_id is not None:
            filters.append(C.business_id == business_id)
        if run_id is not None:
            filters.append(C.run_id == run_id)
        if approval_status is not None:
            filters.append(C.approval_status == approval_status)
        if risk_decision is not None:
            filters.append(C.risk_decision == risk_decision)
        if search is not None:
            like = f"%{search}%"
            filters.append(C.beneficiary_name.ilike(like))
        if from_date is not None:
            filters.append(
                C.created_at >= datetime.datetime.combine(from_date, datetime.time.min)
            )
        if to_date is not None:
            filters.append(
                C.created_at <= datetime.datetime.combine(to_date, datetime.time.max)
            )
        if filters:
            stmt = stmt.where(and_(*filters))
        return stmt

    async def list_all(
        self,
        *,
        business_id: Optional[UUID] = None,
        run_id: Optional[UUID] = None,
        approval_status: Optional[str] = None,
        risk_decision: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[datetime.date] = None,
        to_date: Optional[datetime.date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[PayoutCandidateModel], int]:
        """Return (rows, total_count) for the global approvals queue."""
        C = PayoutCandidateModel
        base = select(C).order_by(C.created_at.desc())
        base = self._apply_filters(
            base,
            business_id=business_id,
            run_id=run_id,
            approval_status=approval_status,
            risk_decision=risk_decision,
            search=search,
            from_date=from_date,
            to_date=to_date,
        )
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_stmt)).scalar_one()
        rows_stmt = base.limit(limit).offset(offset)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    # ── Historical query methods for risk analysis (Phase 4) ──────

    async def get_historical_payouts_to_account(
        self,
        business_id: UUID,
        account_number: str,
        days: int = 30,
        exclude_run_id: Optional[UUID] = None,
    ) -> list[PayoutCandidateModel]:
        """
        Get past payouts to a specific account for velocity/history analysis.

        Args:
            business_id: The business ID to scope the query
            account_number: The account number to search for
            days: Number of days to look back (default 30)
            exclude_run_id: Optional run ID to exclude (current run)

        Returns:
            List of historical payout candidates to this account
        """
        C = PayoutCandidateModel
        cutoff = _utc_now() - datetime.timedelta(days=days)

        filters = [
            C.business_id == business_id,
            C.account_number == account_number,
            C.created_at >= cutoff,
        ]
        if exclude_run_id is not None:
            filters.append(C.run_id != exclude_run_id)

        stmt = select(C).where(and_(*filters)).order_by(C.created_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_name_variations_for_account(
        self,
        business_id: UUID,
        account_number: str,
        exclude_run_id: Optional[UUID] = None,
    ) -> list[str]:
        """
        Get all distinct beneficiary names used for an account historically.

        Used for detecting name inconsistencies (same account, different names).

        Args:
            business_id: The business ID to scope the query
            account_number: The account number to search for
            exclude_run_id: Optional run ID to exclude (current run)

        Returns:
            List of distinct beneficiary names used for this account
        """
        C = PayoutCandidateModel

        filters = [
            C.business_id == business_id,
            C.account_number == account_number,
        ]
        if exclude_run_id is not None:
            filters.append(C.run_id != exclude_run_id)

        stmt = select(func.distinct(C.beneficiary_name)).where(and_(*filters))
        result = await self._session.execute(stmt)
        return [name for name in result.scalars().all() if name]

    async def get_first_payout_date_for_account(
        self,
        business_id: UUID,
        account_number: str,
    ) -> Optional[datetime.datetime]:
        """
        Get the date of the first payout to an account (account age).

        Args:
            business_id: The business ID to scope the query
            account_number: The account number to search for

        Returns:
            The created_at timestamp of the earliest payout, or None if new
        """
        C = PayoutCandidateModel

        stmt = select(func.min(C.created_at)).where(
            C.business_id == business_id,
            C.account_number == account_number,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_last_payout_date_for_account(
        self,
        business_id: UUID,
        account_number: str,
        exclude_run_id: Optional[UUID] = None,
    ) -> Optional[datetime.datetime]:
        """
        Get the date of the most recent payout to an account.

        Args:
            business_id: The business ID to scope the query
            account_number: The account number to search for
            exclude_run_id: Optional run ID to exclude (current run)

        Returns:
            The created_at timestamp of the most recent payout, or None
        """
        C = PayoutCandidateModel

        filters = [
            C.business_id == business_id,
            C.account_number == account_number,
        ]
        if exclude_run_id is not None:
            filters.append(C.run_id != exclude_run_id)

        stmt = select(func.max(C.created_at)).where(and_(*filters))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_average_amount_for_account(
        self,
        business_id: UUID,
        account_number: str,
        exclude_run_id: Optional[UUID] = None,
    ) -> Optional[Decimal]:
        """
        Get the average payout amount to an account historically.

        Args:
            business_id: The business ID to scope the query
            account_number: The account number to search for
            exclude_run_id: Optional run ID to exclude (current run)

        Returns:
            The average amount, or None if no history
        """
        C = PayoutCandidateModel

        filters = [
            C.business_id == business_id,
            C.account_number == account_number,
        ]
        if exclude_run_id is not None:
            filters.append(C.run_id != exclude_run_id)

        stmt = select(func.avg(C.amount)).where(and_(*filters))
        result = await self._session.execute(stmt)
        avg = result.scalar_one_or_none()
        return Decimal(str(avg)) if avg is not None else None

    async def count_payouts_to_account(
        self,
        business_id: UUID,
        account_number: str,
        days: Optional[int] = None,
        exclude_run_id: Optional[UUID] = None,
    ) -> int:
        """
        Count historical payouts to an account.

        Args:
            business_id: The business ID to scope the query
            account_number: The account number to search for
            days: Optional number of days to look back (None = all time)
            exclude_run_id: Optional run ID to exclude (current run)

        Returns:
            Count of payouts to this account
        """
        C = PayoutCandidateModel

        filters = [
            C.business_id == business_id,
            C.account_number == account_number,
        ]
        if days is not None:
            cutoff = _utc_now() - datetime.timedelta(days=days)
            filters.append(C.created_at >= cutoff)
        if exclude_run_id is not None:
            filters.append(C.run_id != exclude_run_id)

        stmt = select(func.count()).select_from(C).where(and_(*filters))
        result = await self._session.execute(stmt)
        return result.scalar_one()

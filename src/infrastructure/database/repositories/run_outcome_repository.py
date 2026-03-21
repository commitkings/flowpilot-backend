"""Repository for run outcome memory persistence and queries."""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select, func, and_, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import (
    RunOutcomeMemoryModel,
)


class RunOutcomeRepository:
    """Manages RunOutcomeMemory persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_batch(
        self,
        run_id: UUID,
        business_id: UUID,
        outcomes: list[dict],
    ) -> list[RunOutcomeMemoryModel]:
        """
        Bulk insert outcome records for a completed run.

        Args:
            run_id: The agent run ID
            business_id: The business ID
            outcomes: List of outcome dicts with keys:
                - candidate_account_number (required)
                - candidate_bank_code (required)
                - amount (required)
                - outcome (required): 'success', 'failed', 'rejected', 'pending', 'skipped'
                - candidate_name (optional)
                - failure_reason (optional)
                - risk_score (optional)
                - risk_decision (optional)
                - execution_duration_ms (optional)

        Returns:
            List of created RunOutcomeMemoryModel instances
        """
        if not outcomes:
            return []

        models = [
            RunOutcomeMemoryModel(
                run_id=run_id,
                business_id=business_id,
                candidate_account_number=outcome["candidate_account_number"],
                candidate_bank_code=outcome["candidate_bank_code"],
                amount=outcome["amount"],
                outcome=outcome["outcome"],
                candidate_name=outcome.get("candidate_name"),
                failure_reason=outcome.get("failure_reason"),
                risk_score=outcome.get("risk_score"),
                risk_decision=outcome.get("risk_decision"),
                execution_duration_ms=outcome.get("execution_duration_ms"),
            )
            for outcome in outcomes
        ]
        self._session.add_all(models)
        await self._session.flush()
        return models

    async def get_by_run(self, run_id: UUID) -> list[RunOutcomeMemoryModel]:
        """Get all outcomes for a specific run."""
        stmt = (
            select(RunOutcomeMemoryModel)
            .where(RunOutcomeMemoryModel.run_id == run_id)
            .order_by(RunOutcomeMemoryModel.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_beneficiary(
        self,
        account_number: str,
        bank_code: str,
        limit: int = 20,
        business_id: Optional[UUID] = None,
    ) -> list[RunOutcomeMemoryModel]:
        """
        Get historical outcomes for a specific beneficiary.

        Args:
            account_number: The account number
            bank_code: The bank code
            limit: Maximum records to return
            business_id: Optional business scope

        Returns:
            List of historical outcomes, most recent first
        """
        M = RunOutcomeMemoryModel
        filters = [
            M.candidate_account_number == account_number,
            M.candidate_bank_code == bank_code,
        ]
        if business_id is not None:
            filters.append(M.business_id == business_id)

        stmt = (
            select(M)
            .where(and_(*filters))
            .order_by(M.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_business_stats(
        self,
        business_id: UUID,
        days_back: int = 30,
    ) -> dict:
        """
        Get aggregated run statistics for a business.

        Args:
            business_id: The business ID
            days_back: Number of days to look back

        Returns:
            Dict with:
                - total_runs: int
                - total_candidates: int
                - success_count: int
                - failed_count: int
                - success_rate: float
                - total_amount: Decimal
                - failure_breakdown: dict[reason -> count]
        """
        M = RunOutcomeMemoryModel
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days_back)

        # Count distinct runs
        run_count_stmt = (
            select(func.count(func.distinct(M.run_id)))
            .where(M.business_id == business_id, M.created_at >= cutoff)
        )
        total_runs = (await self._session.execute(run_count_stmt)).scalar_one()

        # Count outcomes by status
        outcome_counts_stmt = (
            select(M.outcome, func.count().label("cnt"))
            .where(M.business_id == business_id, M.created_at >= cutoff)
            .group_by(M.outcome)
        )
        outcome_result = await self._session.execute(outcome_counts_stmt)
        outcome_counts = {row.outcome: row.cnt for row in outcome_result}

        total_candidates = sum(outcome_counts.values())
        success_count = outcome_counts.get("success", 0)
        failed_count = outcome_counts.get("failed", 0)
        success_rate = (
            success_count / total_candidates if total_candidates > 0 else 0.0
        )

        # Total amount
        amount_stmt = (
            select(func.sum(M.amount))
            .where(M.business_id == business_id, M.created_at >= cutoff)
        )
        total_amount = (await self._session.execute(amount_stmt)).scalar_one() or Decimal("0.00")

        # Failure reasons breakdown
        failure_stmt = (
            select(M.failure_reason, func.count().label("cnt"))
            .where(
                M.business_id == business_id,
                M.created_at >= cutoff,
                M.outcome == "failed",
                M.failure_reason.isnot(None),
            )
            .group_by(M.failure_reason)
        )
        failure_result = await self._session.execute(failure_stmt)
        failure_breakdown = {row.failure_reason: row.cnt for row in failure_result}

        return {
            "total_runs": total_runs,
            "total_candidates": total_candidates,
            "success_count": success_count,
            "failed_count": failed_count,
            "success_rate": success_rate,
            "total_amount": total_amount,
            "failure_breakdown": failure_breakdown,
        }

    async def count_by_business(
        self,
        business_id: UUID,
        outcome: Optional[str] = None,
    ) -> int:
        """Count outcomes for a business, optionally filtered by outcome status."""
        M = RunOutcomeMemoryModel
        filters = [M.business_id == business_id]
        if outcome is not None:
            filters.append(M.outcome == outcome)

        stmt = select(func.count()).select_from(M).where(and_(*filters))
        result = await self._session.execute(stmt)
        return result.scalar_one()

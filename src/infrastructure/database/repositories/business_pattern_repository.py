"""Repository for business pattern profile analysis."""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select, func, and_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.infrastructure.database.flowpilot_models import (
    BusinessPatternProfileModel,
    RunOutcomeMemoryModel,
)


class BusinessPatternRepository:
    """Manages BusinessPatternProfile persistence and analysis."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_profile(
        self,
        business_id: UUID,
    ) -> Optional[BusinessPatternProfileModel]:
        """
        Get pattern profile for a business.

        Args:
            business_id: The business ID

        Returns:
            BusinessPatternProfileModel or None if not found
        """
        stmt = select(BusinessPatternProfileModel).where(
            BusinessPatternProfileModel.business_id == business_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_profile(
        self,
        business_id: UUID,
    ) -> BusinessPatternProfileModel:
        """
        Recalculate and update pattern profile from run_outcome_memory.

        This aggregates all historical outcomes for a business to compute:
        - Total runs and payouts
        - Amount statistics (mean, percentiles)
        - Success rates
        - Failure reason frequencies
        - Recurring beneficiary rates

        Args:
            business_id: The business ID

        Returns:
            Updated or created BusinessPatternProfileModel
        """
        M = RunOutcomeMemoryModel

        # Calculate basic aggregates
        basic_stats = await self._session.execute(
            select(
                func.count(func.distinct(M.run_id)).label("total_runs"),
                func.count().label("total_payouts"),
                func.sum(M.amount).label("total_amount"),
                func.avg(M.amount).label("avg_amount"),
                func.stddev_pop(M.amount).label("amount_std"),
            ).where(M.business_id == business_id)
        )
        stats = basic_stats.one()

        total_runs = stats.total_runs or 0
        total_payouts = stats.total_payouts or 0
        total_amount = stats.total_amount or Decimal("0.00")
        avg_amount = stats.avg_amount
        amount_std = stats.amount_std

        # Average candidates per run
        avg_candidates_per_run = (
            Decimal(str(total_payouts / total_runs)) if total_runs > 0 else None
        )

        # Calculate percentiles using PostgreSQL percentile_cont
        percentile_stmt = select(
            func.percentile_cont(0.25).within_group(M.amount).label("p25"),
            func.percentile_cont(0.50).within_group(M.amount).label("p50"),
            func.percentile_cont(0.75).within_group(M.amount).label("p75"),
            func.percentile_cont(0.95).within_group(M.amount).label("p95"),
        ).where(M.business_id == business_id)

        percentile_result = await self._session.execute(percentile_stmt)
        percentiles = percentile_result.one()

        # Success rate
        success_count_stmt = select(func.count()).where(
            M.business_id == business_id,
            M.outcome == "success",
        )
        success_count = (await self._session.execute(success_count_stmt)).scalar_one()
        success_rate = (
            Decimal(str(success_count / total_payouts))
            if total_payouts > 0
            else Decimal("0.0000")
        )

        # Failure reasons breakdown
        failure_breakdown = await self._get_failure_breakdown(business_id)

        # Recurring beneficiary rate
        recurring_rate = await self._calculate_recurring_rate(business_id)

        # Get last run timestamp
        last_run_stmt = select(func.max(M.created_at)).where(
            M.business_id == business_id
        )
        last_run_at = (await self._session.execute(last_run_stmt)).scalar_one()

        # Upsert the profile
        profile_values = {
            "business_id": business_id,
            "total_runs": total_runs,
            "total_payouts": total_payouts,
            "total_amount_paid": total_amount,
            "avg_candidates_per_run": avg_candidates_per_run,
            "avg_amount_per_candidate": (
                Decimal(str(avg_amount)) if avg_amount else None
            ),
            "amount_std_dev": (
                Decimal(str(amount_std)) if amount_std else None
            ),
            "amount_p25": (
                Decimal(str(percentiles.p25)) if percentiles.p25 else None
            ),
            "amount_p50": (
                Decimal(str(percentiles.p50)) if percentiles.p50 else None
            ),
            "amount_p75": (
                Decimal(str(percentiles.p75)) if percentiles.p75 else None
            ),
            "amount_p95": (
                Decimal(str(percentiles.p95)) if percentiles.p95 else None
            ),
            "overall_success_rate": success_rate,
            "common_failure_reasons": failure_breakdown,
            "recurring_beneficiary_rate": recurring_rate,
            "last_run_at": last_run_at,
        }

        stmt = pg_insert(BusinessPatternProfileModel).values(**profile_values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["business_id"],
            set_={
                **{k: v for k, v in profile_values.items() if k != "business_id"},
                "updated_at": func.now(),
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()

        return await self.get_profile(business_id)

    async def _get_failure_breakdown(
        self,
        business_id: UUID,
    ) -> dict:
        """Get failure reason counts as a dict."""
        M = RunOutcomeMemoryModel
        stmt = (
            select(M.failure_reason, func.count().label("cnt"))
            .where(
                M.business_id == business_id,
                M.outcome == "failed",
                M.failure_reason.isnot(None),
            )
            .group_by(M.failure_reason)
            .order_by(func.count().desc())
        )
        result = await self._session.execute(stmt)
        return {row.failure_reason: row.cnt for row in result}

    async def _calculate_recurring_rate(
        self,
        business_id: UUID,
    ) -> Optional[Decimal]:
        """
        Calculate the rate of recurring beneficiaries.

        Recurring = beneficiaries who appear in more than one run.
        """
        M = RunOutcomeMemoryModel

        # Total unique beneficiaries
        total_unique_stmt = select(
            func.count(
                func.distinct(
                    func.concat(M.candidate_account_number, "-", M.candidate_bank_code)
                )
            )
        ).where(M.business_id == business_id)
        total_unique = (await self._session.execute(total_unique_stmt)).scalar_one()

        if total_unique == 0:
            return None

        # Beneficiaries appearing in multiple runs
        recurring_stmt = text("""
            SELECT COUNT(*) FROM (
                SELECT candidate_account_number, candidate_bank_code
                FROM run_outcome_memory
                WHERE business_id = :business_id
                GROUP BY candidate_account_number, candidate_bank_code
                HAVING COUNT(DISTINCT run_id) > 1
            ) AS recurring
        """)
        recurring_count = (
            await self._session.execute(
                recurring_stmt, {"business_id": str(business_id)}
            )
        ).scalar_one()

        return Decimal(str(recurring_count / total_unique))

    async def is_amount_anomalous(
        self,
        business_id: UUID,
        amount: Decimal,
        z_threshold: float = 2.0,
    ) -> tuple[bool, Optional[float]]:
        """
        Check if an amount is anomalous for this business.

        Uses z-score: (amount - mean) / std_dev

        Args:
            business_id: The business ID
            amount: The amount to check
            z_threshold: Z-score threshold for anomaly (default 2.0)

        Returns:
            Tuple of (is_anomalous, z_score) or (False, None) if insufficient data
        """
        profile = await self.get_profile(business_id)
        if not profile or not profile.avg_amount_per_candidate or not profile.amount_std_dev:
            return False, None

        if profile.amount_std_dev == 0:
            # No variance, any deviation is potentially anomalous
            is_diff = amount != profile.avg_amount_per_candidate
            return is_diff, None

        z_score = float(
            (amount - profile.avg_amount_per_candidate) / profile.amount_std_dev
        )
        return abs(z_score) > z_threshold, z_score

    async def get_active_businesses(
        self,
        days: int = 30,
    ) -> list[UUID]:
        """
        Get business IDs with run outcomes in the given period.

        Args:
            days: Number of days to look back

        Returns:
            List of business IDs with recent activity
        """
        M = RunOutcomeMemoryModel
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)

        stmt = select(func.distinct(M.business_id)).where(M.created_at >= cutoff)
        result = await self._session.execute(stmt)
        return [row[0] for row in result.all()]

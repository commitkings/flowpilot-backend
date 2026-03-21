"""Repository for beneficiary reputation tracking."""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select, func, and_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.infrastructure.database.flowpilot_models import (
    BeneficiaryReputationModel,
)


class BeneficiaryReputationRepository:
    """Manages BeneficiaryReputation persistence and retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_reputation(
        self,
        account_number: str,
        bank_code: str,
    ) -> Optional[BeneficiaryReputationModel]:
        """
        Get reputation record for a beneficiary.

        Args:
            account_number: The account number
            bank_code: The bank code

        Returns:
            BeneficiaryReputationModel or None if not found
        """
        stmt = select(BeneficiaryReputationModel).where(
            BeneficiaryReputationModel.account_number == account_number,
            BeneficiaryReputationModel.bank_code == bank_code,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_batch_reputations(
        self,
        accounts: list[tuple[str, str]],
    ) -> dict[tuple[str, str], BeneficiaryReputationModel]:
        """
        Get reputation records for multiple beneficiaries.

        Args:
            accounts: List of (account_number, bank_code) tuples

        Returns:
            Dict keyed by (account_number, bank_code) -> BeneficiaryReputationModel
        """
        if not accounts:
            return {}

        M = BeneficiaryReputationModel
        # Build OR conditions for each (account, bank) pair
        conditions = [
            and_(M.account_number == acct, M.bank_code == bank)
            for acct, bank in accounts
        ]
        stmt = select(M).where(func.or_(*conditions))
        result = await self._session.execute(stmt)

        return {
            (row.account_number, row.bank_code): row
            for row in result.scalars().all()
        }

    async def upsert_reputation(
        self,
        account_number: str,
        bank_code: str,
        outcome: str,
        amount: Decimal,
        name: Optional[str] = None,
        failure_reason: Optional[str] = None,
    ) -> BeneficiaryReputationModel:
        """
        Update or insert reputation record after a payout attempt.

        Uses PostgreSQL UPSERT (INSERT ... ON CONFLICT) for atomicity.

        Args:
            account_number: The account number
            bank_code: The bank code
            outcome: 'success', 'failed', 'rejected', 'pending', 'skipped'
            amount: The payout amount
            name: Optional beneficiary name
            failure_reason: Optional failure reason if outcome is 'failed'

        Returns:
            Updated or created BeneficiaryReputationModel
        """
        is_success = outcome == "success"
        is_failure = outcome == "failed"

        # Prepare insert values
        insert_values = {
            "account_number": account_number,
            "bank_code": bank_code,
            "beneficiary_name": name,
            "total_attempts": 1,
            "successful_payouts": 1 if is_success else 0,
            "failed_payouts": 1 if is_failure else 0,
            "success_rate": Decimal("1.0000") if is_success else Decimal("0.0000"),
            "total_amount_paid": amount if is_success else Decimal("0.00"),
            "average_amount": amount if is_success else None,
            "last_outcome": outcome,
            "last_failure_reason": failure_reason if is_failure else None,
            "last_payout_at": datetime.datetime.utcnow() if is_success else None,
            "reputation_score": self._compute_initial_score(is_success),
        }

        # Build upsert statement
        stmt = pg_insert(BeneficiaryReputationModel).values(**insert_values)

        # On conflict, update the existing record
        M = BeneficiaryReputationModel
        update_dict = {
            "total_attempts": M.total_attempts + 1,
            "last_outcome": outcome,
            "updated_at": func.now(),
        }

        if is_success:
            update_dict["successful_payouts"] = M.successful_payouts + 1
            update_dict["total_amount_paid"] = M.total_amount_paid + amount
            update_dict["last_payout_at"] = func.now()
            # Recalculate average: (old_avg * old_success_count + new_amount) / new_success_count
            update_dict["average_amount"] = (
                func.coalesce(M.average_amount, Decimal("0.00")) * M.successful_payouts + amount
            ) / (M.successful_payouts + 1)
        elif is_failure:
            update_dict["failed_payouts"] = M.failed_payouts + 1
            update_dict["last_failure_reason"] = failure_reason

        # Recalculate success_rate
        new_successes = (
            M.successful_payouts + 1 if is_success else M.successful_payouts
        )
        new_total = M.total_attempts + 1
        update_dict["success_rate"] = func.cast(new_successes, Decimal) / func.cast(
            new_total, Decimal
        )

        # Update beneficiary name if provided and currently null
        if name:
            update_dict["beneficiary_name"] = func.coalesce(
                M.beneficiary_name, name
            )

        # Recalculate reputation score using the formula
        update_dict["reputation_score"] = self._sql_reputation_formula(
            new_successes, new_total
        )

        stmt = stmt.on_conflict_do_update(
            constraint="beneficiary_reputation_account_bank_uq",
            set_=update_dict,
        )
        await self._session.execute(stmt)
        await self._session.flush()

        # Return the updated record
        return await self.get_reputation(account_number, bank_code)

    def _compute_initial_score(self, is_success: bool) -> Decimal:
        """
        Compute initial reputation score for a new beneficiary.

        First-time beneficiaries start at 0.5 (neutral) with adjustment
        based on first outcome.
        """
        if is_success:
            return Decimal("0.6000")  # Slight positive for first success
        return Decimal("0.4000")  # Slight negative for first failure

    def _sql_reputation_formula(self, successes, total):
        """
        SQL expression for reputation score calculation.

        Formula: base_rate * confidence + prior * (1 - confidence)
        - base_rate = success_rate
        - confidence = min(total_attempts / 10, 1.0)  # Full confidence after 10 attempts
        - prior = 0.5 (neutral)

        This Bayesian-inspired approach prevents extreme scores with
        limited data while converging to actual success rate over time.
        """
        confidence = func.least(func.cast(total, Decimal) / 10, Decimal("1.0"))
        base_rate = func.cast(successes, Decimal) / func.cast(total, Decimal)
        prior = Decimal("0.5")
        return base_rate * confidence + prior * (1 - confidence)

    async def get_low_reputation_beneficiaries(
        self,
        threshold: Decimal = Decimal("0.4"),
        min_attempts: int = 3,
        limit: int = 100,
    ) -> list[BeneficiaryReputationModel]:
        """
        Get beneficiaries with low reputation scores.

        Args:
            threshold: Maximum reputation score to include
            min_attempts: Minimum attempts required
            limit: Maximum records to return

        Returns:
            List of low-reputation beneficiaries
        """
        M = BeneficiaryReputationModel
        stmt = (
            select(M)
            .where(
                M.reputation_score <= threshold,
                M.total_attempts >= min_attempts,
            )
            .order_by(M.reputation_score.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_statistics(self) -> dict:
        """Get global reputation statistics."""
        M = BeneficiaryReputationModel

        stats_stmt = select(
            func.count().label("total_beneficiaries"),
            func.avg(M.reputation_score).label("avg_reputation"),
            func.avg(M.success_rate).label("avg_success_rate"),
            func.sum(M.total_attempts).label("total_payouts"),
            func.sum(M.total_amount_paid).label("total_amount"),
        )
        result = (await self._session.execute(stats_stmt)).one()

        return {
            "total_beneficiaries": result.total_beneficiaries or 0,
            "avg_reputation": float(result.avg_reputation or 0),
            "avg_success_rate": float(result.avg_success_rate or 0),
            "total_payouts": result.total_payouts or 0,
            "total_amount": result.total_amount or Decimal("0.00"),
        }

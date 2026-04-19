"""KYC tier limit lookup — DB-first with fallback to hardcoded config."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.kyc_limits import KYC_LIMITS, LimitTier, get_limits as _hardcoded
from src.infrastructure.database.flowpilot_models import KycTierLimitModel


async def get_limits(
    session: AsyncSession,
    account_type: str,
    kyc_level: int,
) -> Optional[LimitTier]:
    """Return the effective KYC limit tier for account_type × kyc_level.

    Queries the kyc_tier_limit table for the most recently effective row
    (effective_from ≤ today, effective_to IS NULL or in the future).
    Falls back to the hardcoded kyc_limits.py values if the table has no row.
    """
    today = date.today()
    result = await session.execute(
        select(KycTierLimitModel)
        .where(
            KycTierLimitModel.account_type == account_type,
            KycTierLimitModel.kyc_level == kyc_level,
            KycTierLimitModel.effective_from <= today,
            (KycTierLimitModel.effective_to == None) | (KycTierLimitModel.effective_to > today),  # noqa: E711
        )
        .order_by(KycTierLimitModel.effective_from.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return LimitTier(
            monthly=Decimal(str(row.monthly_limit)),
            single=Decimal(str(row.single_txn_limit)),
            wallet=Decimal(str(row.wallet_balance_limit)),
        )

    return _hardcoded(account_type, kyc_level)


def get_max_level(account_type: str) -> int:
    return max(KYC_LIMITS.get(account_type, {}).keys(), default=0)

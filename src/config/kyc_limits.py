"""
KYC tier limits — defines monthly payout allowance, single transaction cap,
and maximum wallet balance for each account type × KYC level combination.

Limits (all in NGN):
  Individual L1 (NIN / BVN):              monthly 300k,  single 50k,   wallet 500k
  Individual L2 (+ proof of address):     monthly 1m,    single 200k,  wallet 2m
  Individual L3 (+ government photo ID):  monthly 3m,    single 500k,  wallet 5m

  Business L1 (BVN + CAC):               monthly 1.5m,  single 300k,  wallet 3m
  Business L2 (+ TIN + proof of address): monthly 10m,   single 2m,    wallet 20m
  Business L3 (full docs):                monthly 50m,   single 5m,    wallet 100m

Beyond Level 3, users must contact support.
"""

from decimal import Decimal
from typing import TypedDict


SUPPORT_EMAIL = "support@flowpilot.ng"


class LimitTier(TypedDict):
    monthly: Decimal   # max cumulative successful payouts in a calendar month
    single: Decimal    # max amount for a single payout
    wallet: Decimal    # max wallet balance allowed


# { account_type: { kyc_level: LimitTier } }
KYC_LIMITS: dict[str, dict[int, LimitTier]] = {
    "individual": {
        1: {
            "monthly": Decimal("300000"),
            "single": Decimal("50000"),
            "wallet": Decimal("500000"),
        },
        2: {
            "monthly": Decimal("1000000"),
            "single": Decimal("200000"),
            "wallet": Decimal("2000000"),
        },
        3: {
            "monthly": Decimal("3000000"),
            "single": Decimal("500000"),
            "wallet": Decimal("5000000"),
        },
    },
    "business": {
        1: {
            "monthly": Decimal("1500000"),
            "single": Decimal("300000"),
            "wallet": Decimal("3000000"),
        },
        2: {
            "monthly": Decimal("10000000"),
            "single": Decimal("2000000"),
            "wallet": Decimal("20000000"),
        },
        3: {
            "monthly": Decimal("50000000"),
            "single": Decimal("5000000"),
            "wallet": Decimal("100000000"),
        },
    },
}


def get_limits(account_type: str, kyc_level: int) -> LimitTier | None:
    """Return the limit tier for the given account type and level, or None if level=0."""
    return KYC_LIMITS.get(account_type, {}).get(kyc_level)


def get_max_level(account_type: str) -> int:
    return max(KYC_LIMITS.get(account_type, {}).keys(), default=0)

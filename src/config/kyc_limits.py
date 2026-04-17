"""
KYC tier limits — defines monthly payout allowance, single transaction cap,
and maximum wallet balance for each account type × KYC level combination.

Limits (all in NGN):
  Individual L1 (NIN / BVN):              monthly 500k,   single 100k,  wallet 1m
  Individual L2 (+ proof of address):     monthly 2m,     single 500k,  wallet 4m
  Individual L3 (+ government photo ID):  monthly 5m,     single 1.5m,  wallet 10m

  Business L1 (BVN + CAC):               monthly 5m,     single 2m,    wallet 10m
  Business L2 (+ TIN + proof of address): monthly 30m,    single 10m,   wallet 60m
  Business L3 (full docs):                monthly 100m,   single 20m,   wallet 200m

Beyond Level 3, users must contact support.

Rationale: FlowPilot is a bulk payroll platform. Limits are sized so that
Business L1 covers small teams (≤25 staff), L2 covers mid-size payroll
(≤100 staff), and L3 covers large/enterprise payroll (≤500 staff).
"""

from decimal import Decimal
from typing import TypedDict


SUPPORT_EMAIL = "support@flowpilot.ng"


class LimitTier(TypedDict):
    monthly: Decimal   # max cumulative successful payouts in a calendar month
    single: Decimal    # max amount for a single payout run (batch total)
    wallet: Decimal    # max wallet balance allowed


# { account_type: { kyc_level: LimitTier } }
KYC_LIMITS: dict[str, dict[int, LimitTier]] = {
    "individual": {
        1: {
            "monthly": Decimal("500000"),
            "single":  Decimal("100000"),
            "wallet":  Decimal("1000000"),
        },
        2: {
            "monthly": Decimal("2000000"),
            "single":  Decimal("500000"),
            "wallet":  Decimal("4000000"),
        },
        3: {
            "monthly": Decimal("5000000"),
            "single":  Decimal("1500000"),
            "wallet":  Decimal("10000000"),
        },
    },
    "business": {
        1: {
            "monthly": Decimal("5000000"),
            "single":  Decimal("2000000"),
            "wallet":  Decimal("10000000"),
        },
        2: {
            "monthly": Decimal("30000000"),
            "single":  Decimal("10000000"),
            "wallet":  Decimal("60000000"),
        },
        3: {
            "monthly": Decimal("100000000"),
            "single":  Decimal("20000000"),
            "wallet":  Decimal("200000000"),
        },
    },
}


def get_limits(account_type: str, kyc_level: int) -> LimitTier | None:
    """Return the limit tier for the given account type and level, or None if level=0."""
    return KYC_LIMITS.get(account_type, {}).get(kyc_level)


def get_max_level(account_type: str) -> int:
    return max(KYC_LIMITS.get(account_type, {}).keys(), default=0)

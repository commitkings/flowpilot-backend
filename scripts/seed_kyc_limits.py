#!/usr/bin/env python3
"""
Seed CBN KYC tier limits into the kyc_tier_limit table.

Amounts are in kobo (NGN × 100). Idempotent: ON CONFLICT DO NOTHING.

Usage:
    python scripts/seed_kyc_limits.py
"""

import asyncio
import os
import sys
from datetime import date
from pathlib import Path

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parent.parent

env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:oracle@localhost:5432/flowpilot"
)

if DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
elif DATABASE_URL.startswith("postgres+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgres+asyncpg://", "postgresql://", 1)

# (account_type, kyc_level, single_txn_limit, monthly_limit, wallet_balance_limit)
# All amounts in NGN (not kobo).
LIMITS = [
    ("individual", 1,    100_000,    500_000,   1_000_000),
    ("individual", 2,    500_000,  2_000_000,   4_000_000),
    ("individual", 3,  1_500_000,  5_000_000,  10_000_000),
    ("business",   1,  2_000_000,  5_000_000,  10_000_000),
    ("business",   2, 10_000_000, 30_000_000,  60_000_000),
    ("business",   3, 20_000_000,100_000_000, 200_000_000),
]


async def main():
    print("Connecting to database...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}")
        sys.exit(1)

    today = date.today()
    seeded = 0

    async with conn.transaction():
        for account_type, kyc_level, single, monthly, wallet in LIMITS:
            result = await conn.execute(
                """
                INSERT INTO kyc_tier_limit
                    (account_type, kyc_level, single_txn_limit, monthly_limit,
                     wallet_balance_limit, effective_from)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (account_type, kyc_level, effective_from) DO NOTHING
                """,
                account_type, kyc_level, single, monthly, wallet, today,
            )
            if result == "INSERT 0 1":
                seeded += 1

    await conn.close()
    print(f"Done. {seeded} KYC tier limit rows inserted (rest already existed).")


if __name__ == "__main__":
    asyncio.run(main())

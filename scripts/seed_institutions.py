#!/usr/bin/env python3
"""
Seed Nigerian institutions into FlowPilot from the Monnify /banks API.

Falls back to a hardcoded CBN/NIP list if Monnify credentials are missing
or the API is unreachable at startup time.

Idempotent: ON CONFLICT (institution_code) DO UPDATE so re-running is safe.

Usage:
    python scripts/seed_institutions.py
"""

import asyncio
import base64
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg
import httpx

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

MONNIFY_BASE_URL = os.environ.get("MONNIFY_BASE_URL", "https://api.monnify.com").rstrip("/")
MONNIFY_API_KEY = os.environ.get("MONNIFY_API_KEY", "")
MONNIFY_SECRET_KEY = os.environ.get("MONNIFY_SECRET_KEY", "")

# ---------------------------------------------------------------------------
# Static fallback — used when Monnify is unavailable at seeding time.
# Each entry: (institution_code, institution_name, short_name, nip_code, cbn_code, institution_type)
# ---------------------------------------------------------------------------
_FALLBACK = [
    ("011", "First Bank of Nigeria",               "First Bank",       "011", "011", "bank"),
    ("023", "Citibank Nigeria",                    "Citibank",         "023", "023", "bank"),
    ("032", "Union Bank of Nigeria",               "Union Bank",       "032", "032", "bank"),
    ("033", "United Bank for Africa",              "UBA",              "033", "033", "bank"),
    ("035", "Wema Bank",                           "Wema Bank",        "035", "035", "bank"),
    ("044", "Access Bank Nigeria",                 "Access Bank",      "044", "044", "bank"),
    ("050", "Ecobank Nigeria",                     "Ecobank",          "050", "050", "bank"),
    ("057", "Zenith Bank",                         "Zenith Bank",      "057", "057", "bank"),
    ("058", "Guaranty Trust Bank",                 "GTBank",           "058", "058", "bank"),
    ("068", "Standard Chartered Bank Nigeria",     "Std Chartered",    "068", "068", "bank"),
    ("070", "Fidelity Bank Nigeria",               "Fidelity Bank",    "070", "070", "bank"),
    ("076", "Polaris Bank",                        "Polaris Bank",     "076", "076", "bank"),
    ("082", "Keystone Bank",                       "Keystone Bank",    "082", "082", "bank"),
    ("101", "Providus Bank",                       "Providus Bank",    "101", "101", "bank"),
    ("102", "Titan Trust Bank",                    "Titan Trust",      "102", "102", "bank"),
    ("103", "Globus Bank",                         "Globus Bank",      "103", "103", "bank"),
    ("105", "Premium Trust Bank",                  "Premium Trust",    "105", "105", "bank"),
    ("106", "Signature Bank",                      "Signature Bank",   "106", "106", "bank"),
    ("214", "First City Monument Bank",            "FCMB",             "214", "214", "bank"),
    ("215", "Unity Bank",                          "Unity Bank",       "215", "215", "bank"),
    ("221", "Stanbic IBTC Bank",                   "Stanbic IBTC",     "221", "221", "bank"),
    ("232", "Sterling Bank",                       "Sterling Bank",    "232", "232", "bank"),
    ("301", "Jaiz Bank",                           "Jaiz Bank",        "301", "301", "bank"),
    ("304", "Stanbic IBTC (Pension)",              "Stanbic Pension",  "304", "304", "bank"),
    ("315", "Advans La Fayette MFB",               "Advans MFB",       "315", "315", "microfinance"),
    ("326", "Spring Bank",                         "Spring Bank",      "326", "326", "bank"),
    ("401", "Rand Merchant Bank",                  "RMB Nigeria",      "401", "401", "bank"),
    ("415", "SunTrust Bank",                       "SunTrust",         "415", "415", "bank"),
    ("501", "Coronation Merchant Bank",            "Coronation MB",    "501", "501", "bank"),
    ("502", "FSDH Merchant Bank",                  "FSDH MB",          "502", "502", "bank"),
    ("523", "Parallex Bank",                       "Parallex Bank",    "523", "523", "bank"),
    ("526", "Carbon (formerly Paylater)",          "Carbon",           "526", "526", "bank"),
    ("559", "Accion MFB",                          "Accion MFB",       "559", "559", "microfinance"),
    ("090110", "VFD Microfinance Bank",            "VFD MFB",          "090110", "090110", "microfinance"),
    ("090267", "Kuda Microfinance Bank",           "Kuda Bank",        "090267", "090267", "microfinance"),
    ("090326", "Sparkle MFB",                      "Sparkle",          "090326", "090326", "microfinance"),
    ("090405", "Moniepoint MFB",                   "Moniepoint",       "090405", "090405", "microfinance"),
    ("100002", "Pagatech",                         "Paga",             "100002", "100002", "mobile_money"),
    ("100004", "OPay Digital Services",            "OPay",             "100004", "100004", "mobile_money"),
    ("100022", "Flutterwave",                      "Flutterwave",      "100022", "100022", "mobile_money"),
    ("100033", "PalmPay",                          "PalmPay",          "100033", "100033", "mobile_money"),
    ("100034", "Paystack Titan",                   "Paystack",         "100034", "100034", "mobile_money"),
    ("120001", "9 Payment Service Bank",           "9PSB",             "120001", "120001", "mobile_money"),
    ("120003", "MoneyMaster PSB",                  "MoneyMaster",      "120003", "120003", "mobile_money"),
    ("120004", "SmartCash PSB",                    "SmartCash",        "120004", "120004", "mobile_money"),
]


async def _fetch_from_monnify() -> Optional[list[tuple]]:
    """Return list of (code, name, short, nip, cbn, type) from Monnify, or None on failure."""
    if not MONNIFY_API_KEY or not MONNIFY_SECRET_KEY:
        print("Monnify credentials not set — skipping API fetch.")
        return None
    try:
        raw = f"{MONNIFY_API_KEY}:{MONNIFY_SECRET_KEY}".encode()
        basic = base64.b64encode(raw).decode()
        async with httpx.AsyncClient(timeout=20) as client:
            # Authenticate
            auth_resp = await client.post(
                f"{MONNIFY_BASE_URL}/api/v1/auth/login",
                headers={"Authorization": f"Basic {basic}"},
            )
            auth_resp.raise_for_status()
            token = auth_resp.json()["responseBody"]["accessToken"]

            # Fetch banks
            banks_resp = await client.get(
                f"{MONNIFY_BASE_URL}/api/v1/sdk/transactions/banks",
                headers={"Authorization": f"Bearer {token}"},
            )
            banks_resp.raise_for_status()
            banks = banks_resp.json().get("responseBody", [])

        institutions = []
        for b in banks:
            code = str(b.get("code", "")).strip()
            name = str(b.get("name", "")).strip()
            if not code or not name:
                continue
            institutions.append((code, name, None, code, code, "bank"))

        print(f"Monnify returned {len(institutions)} banks.")
        return institutions
    except Exception as exc:
        print(f"Monnify fetch failed ({exc}) — falling back to static list.")
        return None


async def _upsert(conn, rows: list[tuple], now: datetime) -> tuple[int, int]:
    inserted = updated = 0
    async with conn.transaction():
        for code, name, short, nip, cbn, itype in rows:
            result = await conn.execute(
                """
                INSERT INTO institution
                    (institution_code, institution_name, short_name, nip_code, cbn_code,
                     institution_type, is_active, last_synced_at)
                VALUES ($1, $2, $3, $4, $5, $6, true, $7)
                ON CONFLICT (institution_code) DO UPDATE SET
                    institution_name  = EXCLUDED.institution_name,
                    nip_code          = EXCLUDED.nip_code,
                    cbn_code          = EXCLUDED.cbn_code,
                    institution_type  = EXCLUDED.institution_type,
                    is_active         = true,
                    last_synced_at    = EXCLUDED.last_synced_at
                """,
                code, name, short, nip, cbn, itype, now,
            )
            if result.startswith("INSERT"):
                inserted += 1
            else:
                updated += 1
    return inserted, updated


async def main():
    rows = await _fetch_from_monnify()
    if rows is None:
        print("Using static fallback institution list.")
        rows = _FALLBACK

    print("Connecting to database...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    inserted, updated = await _upsert(conn, rows, now)
    await conn.close()

    total = inserted + updated
    source = "Monnify" if rows is not _FALLBACK else "static fallback"
    print(f"Done ({source}). {total} institutions seeded ({inserted} new, {updated} updated).")


if __name__ == "__main__":
    asyncio.run(main())

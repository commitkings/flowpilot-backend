#!/usr/bin/env python3
"""
Seed Nigerian CBN/NIP institutions into the FlowPilot database.

Covers all CBN-licensed commercial banks, merchant banks, non-interest banks,
and major microfinance/digital banks (OPay, Kuda, Moniepoint, PalmPay, etc.).

Usage:
    python scripts/seed_institutions.py

Idempotent: uses ON CONFLICT (institution_code) DO UPDATE so re-running is safe.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
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

# Sanitize DATABASE_URL for asyncpg (it doesn't support +asyncpg scheme)
if DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
elif DATABASE_URL.startswith("postgres+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgres+asyncpg://", "postgresql://", 1)

# ---------------------------------------------------------------------------
# Institution data
# Each entry: (institution_code, institution_name, short_name, nip_code, cbn_code, institution_type)
# institution_code = the code used in Interswitch/NIBSS API calls
# nip_code         = NIBSS Instant Payment bank code
# cbn_code         = CBN sort code prefix (same as NIP code for most banks)
# ---------------------------------------------------------------------------

INSTITUTIONS = [
    # ── Commercial Banks ────────────────────────────────────────────────────
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

    # ── Microfinance & Digital Banks ────────────────────────────────────────
    ("090110", "VFD Microfinance Bank",            "VFD MFB",          "090110", "090110", "microfinance"),
    ("090115", "TCF MFB",                          "TCF MFB",          "090115", "090115", "microfinance"),
    ("090120", "Wetland MFB",                      "Wetland MFB",      "090120", "090120", "microfinance"),
    ("090175", "Rubies MFB",                       "Rubies MFB",       "090175", "090175", "microfinance"),
    ("090177", "Focusmicro MFB",                   "Focusmicro MFB",   "090177", "090177", "microfinance"),
    ("090205", "Newdawn MFB",                      "Newdawn MFB",      "090205", "090205", "microfinance"),
    ("090259", "Alekun MFB",                       "Alekun MFB",       "090259", "090259", "microfinance"),
    ("090267", "Kuda Microfinance Bank",           "Kuda Bank",        "090267", "090267", "microfinance"),
    ("090270", "AB Microfinance Bank",             "AB MFB",           "090270", "090270", "microfinance"),
    ("090281", "Boctrust MFB",                     "Boctrust MFB",     "090281", "090281", "microfinance"),
    ("090291", "Finatrust MFB",                    "Finatrust MFB",    "090291", "090291", "microfinance"),
    ("090303", "Oche MFB",                         "Oche MFB",         "090303", "090303", "microfinance"),
    ("090326", "Sparkle MFB",                      "Sparkle",          "090326", "090326", "microfinance"),
    ("090360", "Cashconnect MFB",                  "Cashconnect MFB",  "090360", "090360", "microfinance"),
    ("090405", "Moniepoint MFB",                   "Moniepoint",       "090405", "090405", "microfinance"),
    ("090529", "Nwannegadi MFB",                   "Nwannegadi MFB",   "090529", "090529", "microfinance"),

    # ── Mobile Money / Fintech (100xxx codes) ───────────────────────────────
    ("100001", "FET (Funds & Electronic Transfer)", "FET",             "100001", "100001", "mobile_money"),
    ("100002", "Pagatech",                         "Paga",             "100002", "100002", "mobile_money"),
    ("100003", "ChamsMobile",                      "ChamsMobile",      "100003", "100003", "mobile_money"),
    ("100004", "OPay Digital Services",            "OPay",             "100004", "100004", "mobile_money"),
    ("100005", "Cellulant",                        "Cellulant",        "100005", "100005", "mobile_money"),
    ("100006", "eTranzact",                        "eTranzact",        "100006", "100006", "mobile_money"),
    ("100007", "Stanbic IBTC (Wallet)",            "Stanbic Wallet",   "100007", "100007", "mobile_money"),
    ("100008", "Ecobank (Xpress Account)",         "Ecobank Xpress",   "100008", "100008", "mobile_money"),
    ("100010", "VTNetworks",                       "VTNetworks",       "100010", "100010", "mobile_money"),
    ("100011", "Mkudi",                            "Mkudi",            "100011", "100011", "mobile_money"),
    ("100012", "TagPay",                           "TagPay",           "100012", "100012", "mobile_money"),
    ("100013", "Fidelity (Mobile Banking)",        "Fidelity Mobile",  "100013", "100013", "mobile_money"),
    ("100014", "GTBank (GTMobile)",                "GTMobile",         "100014", "100014", "mobile_money"),
    ("100022", "Flutterwave",                      "Flutterwave",      "100022", "100022", "mobile_money"),
    ("100025", "Kegow (formerly Chams Switch)",    "Kegow",            "100025", "100025", "mobile_money"),
    ("100026", "One Finance",                      "One Finance",      "100026", "100026", "mobile_money"),
    ("100033", "PalmPay",                          "PalmPay",          "100033", "100033", "mobile_money"),
    ("100034", "Paystack Titan",                   "Paystack",         "100034", "100034", "mobile_money"),
    ("100036", "Pocket by Stanbic IBTC",           "Pocket",           "100036", "100036", "mobile_money"),
    ("110001", "FSDH Merchant Bank (Mobile)",      "FSDH Mobile",      "110001", "110001", "mobile_money"),
    ("120001", "9 Payment Service Bank",           "9PSB",             "120001", "120001", "mobile_money"),
    ("120002", "Hopes PSB",                        "Hopes PSB",        "120002", "120002", "mobile_money"),
    ("120003", "MoneyMaster PSB",                  "MoneyMaster",      "120003", "120003", "mobile_money"),
    ("120004", "SmartCash PSB",                    "SmartCash",        "120004", "120004", "mobile_money"),
]

# ---------------------------------------------------------------------------

async def main():
    print("Connecting to database...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    inserted = 0
    updated = 0

    async with conn.transaction():
        for code, name, short, nip, cbn, itype in INSTITUTIONS:
            result = await conn.execute(
                """
                INSERT INTO institution
                    (institution_code, institution_name, short_name, nip_code, cbn_code,
                     institution_type, is_active, last_synced_at)
                VALUES ($1, $2, $3, $4, $5, $6, true, $7)
                ON CONFLICT (institution_code) DO UPDATE SET
                    institution_name = EXCLUDED.institution_name,
                    short_name       = EXCLUDED.short_name,
                    nip_code         = EXCLUDED.nip_code,
                    cbn_code         = EXCLUDED.cbn_code,
                    institution_type = EXCLUDED.institution_type,
                    is_active        = true,
                    last_synced_at   = EXCLUDED.last_synced_at
                """,
                code, name, short, nip, cbn, itype, now,
            )
            # asyncpg returns "INSERT 0 1" or "UPDATE 1"
            if result.startswith("INSERT"):
                inserted += 1
            else:
                updated += 1

    await conn.close()

    total = inserted + updated
    print(f"Done. {total} institutions seeded ({inserted} new, {updated} updated).")


if __name__ == "__main__":
    asyncio.run(main())

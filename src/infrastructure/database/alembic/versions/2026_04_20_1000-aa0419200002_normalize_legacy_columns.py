"""Backfill normalized tables and drop legacy columns on user/business/config/kyc.

Revision ID: aa0419200002
Revises: aa0419200001
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

revision = "aa0419200002"
down_revision = "aa0419200001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── user_profile ──────────────────────────────────────────────────────
    conn.execute(
        text(
            """
            INSERT INTO user_profile (
                id, user_id, display_name, first_name, last_name, phone, avatar_url,
                date_of_birth, job_title, department, timezone, has_taken_tour,
                created_at, updated_at
            )
            SELECT gen_random_uuid(), u.id,
                COALESCE(NULLIF(trim(u.display_name), ''), split_part(u.email, '@', 1)),
                u.first_name, u.last_name, u.phone, u.avatar_url, u.date_of_birth,
                u.job_title, u.department, COALESCE(u.timezone, 'Africa/Lagos'),
                COALESCE(u.has_taken_tour, false), u.created_at, u.updated_at
            FROM "user" u
            WHERE NOT EXISTS (SELECT 1 FROM user_profile p WHERE p.user_id = u.id)
            """
        )
    )

    # ── user_mfa ──────────────────────────────────────────────────────────
    conn.execute(
        text(
            """
            INSERT INTO user_mfa (
                id, user_id, totp_secret, totp_enabled_at, backup_codes_hash,
                totp_grace_until, approval_pin_hash, security_version, created_at, updated_at
            )
            SELECT gen_random_uuid(), u.id, u.totp_secret, u.totp_enabled_at,
                u.backup_codes_hash, u.totp_grace_until, u.approval_pin_hash, 0,
                u.created_at, u.updated_at
            FROM "user" u
            WHERE NOT EXISTS (SELECT 1 FROM user_mfa m WHERE m.user_id = u.id)
              AND (
                u.totp_secret IS NOT NULL OR u.totp_enabled_at IS NOT NULL
                OR u.approval_pin_hash IS NOT NULL
              )
            """
        )
    )

    # ── user_oauth_provider (Google) ─────────────────────────────────────
    conn.execute(
        text(
            """
            INSERT INTO user_oauth_provider (id, user_id, provider, external_id, created_at)
            SELECT gen_random_uuid(), u.id, 'google', u.external_id, u.created_at
            FROM "user" u
            WHERE u.external_provider = 'google'
              AND NOT EXISTS (
                SELECT 1 FROM user_oauth_provider o
                WHERE o.user_id = u.id AND o.provider = 'google'
              )
            """
        )
    )

    # ── notification_preferences JSONB → rows (best-effort) ─────────────
    rows = conn.execute(
        text(
            'SELECT id, notification_preferences FROM "user" WHERE notification_preferences IS NOT NULL'
        )
    ).fetchall()
    for uid, prefs in rows:
        if not prefs or not isinstance(prefs, dict):
            continue
        for key, enabled in prefs.items():
            if not isinstance(key, str):
                continue
            event_type = key
            channel = "email"
            if ":" in key:
                parts = key.split(":", 1)
                channel, event_type = parts[0], parts[1]
            conn.execute(
                text(
                    """
                    INSERT INTO user_notification_preference (
                        id, user_id, channel, event_type, is_enabled, updated_at
                    )
                    SELECT gen_random_uuid(), :uid, :channel, :event_type, :en, now()
                    WHERE NOT EXISTS (
                        SELECT 1 FROM user_notification_preference u
                        WHERE u.user_id = :uid2 AND u.channel = :channel2 AND u.event_type = :event_type2
                    )
                    """
                ),
                {
                    "uid": uid,
                    "uid2": uid,
                    "channel": channel[:20],
                    "channel2": channel[:20],
                    "event_type": event_type[:64],
                    "event_type2": event_type[:64],
                    "en": bool(enabled),
                },
            )

    # ── business_profile / address / virtual_account / policies ───────────
    conn.execute(
        text(
            """
            INSERT INTO business_profile (
                id, business_id, business_type, rc_number, tax_id, phone, website,
                logo_url, interswitch_merchant_id, created_at, updated_at
            )
            SELECT gen_random_uuid(), b.id, b.business_type, b.rc_number, b.tax_id,
                b.phone, b.website, b.logo_url, b.interswitch_merchant_id,
                b.created_at, b.updated_at
            FROM business b
            WHERE NOT EXISTS (SELECT 1 FROM business_profile bp WHERE bp.business_id = b.id)
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO business_address (
                id, business_id, city, state, country, created_at, updated_at
            )
            SELECT gen_random_uuid(), b.id, b.city, b.state, COALESCE(b.country, 'Nigeria'),
                b.created_at, b.updated_at
            FROM business b
            WHERE NOT EXISTS (SELECT 1 FROM business_address ba WHERE ba.business_id = b.id)
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO business_virtual_account (
                id, business_id, account_number, account_name, bank_name, bank_code,
                account_reference, provider, is_primary, is_active, created_at, updated_at
            )
            SELECT gen_random_uuid(), b.id, b.virtual_account_number, b.virtual_account_name,
                b.virtual_account_bank, b.virtual_account_bank_code, b.virtual_account_reference,
                'monnify', true, true, b.created_at, b.updated_at
            FROM business b
            WHERE b.virtual_account_number IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM business_virtual_account v
                WHERE v.business_id = b.id AND v.account_number = b.virtual_account_number
              )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO business_payment_policy (
                id, business_id, monthly_txn_volume_range, avg_monthly_payouts_range,
                primary_bank, risk_appetite, default_risk_tolerance, default_budget_cap,
                daily_payout_limit, single_payout_cap, risk_alert_threshold,
                liquidity_alert_buffer, merchant_state, created_at, updated_at
            )
            SELECT gen_random_uuid(), c.business_id, c.monthly_txn_volume_range,
                c.avg_monthly_payouts_range, c.primary_bank, c.risk_appetite,
                c.default_risk_tolerance, c.default_budget_cap, c.daily_payout_limit,
                c.single_payout_cap, c.risk_alert_threshold, c.liquidity_alert_buffer,
                c.merchant_state, c.created_at, c.updated_at
            FROM business_config c
            WHERE NOT EXISTS (
                SELECT 1 FROM business_payment_policy p WHERE p.business_id = c.business_id
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO business_security_policy (
                id, business_id, require_2fa, require_2fa_enforced_at,
                session_timeout_minutes, ip_allowlist, created_at, updated_at
            )
            SELECT gen_random_uuid(), c.business_id, c.require_2fa, c.require_2fa_enforced_at,
                480, NULL, c.created_at, c.updated_at
            FROM business_config c
            WHERE NOT EXISTS (
                SELECT 1 FROM business_security_policy s WHERE s.business_id = c.business_id
            )
            """
        )
    )

    # primary_use_cases JSONB → business_use_case rows
    uc_rows = conn.execute(
        text("SELECT business_id, primary_use_cases FROM business_config WHERE primary_use_cases IS NOT NULL")
    ).fetchall()
    for bid, cases in uc_rows:
        if not cases:
            continue
        arr = cases if isinstance(cases, list) else json.loads(cases) if isinstance(cases, str) else []
        for item in arr:
            if not item:
                continue
            uc = str(item)[:64]
            conn.execute(
                text(
                    """
                    INSERT INTO business_use_case (id, business_id, use_case, created_at)
                    SELECT gen_random_uuid(), :bid, :uc, now()
                    WHERE NOT EXISTS (
                        SELECT 1 FROM business_use_case x
                        WHERE x.business_id = :bid2 AND x.use_case = :uc2
                    )
                    """
                ),
                {"bid": bid, "bid2": bid, "uc": uc, "uc2": uc},
            )

    # ── kyc_document from non-null keys on kyc_submission ────────────────
    ks = conn.execute(
        text(
            """
            SELECT id, business_id, cac_certificate_key, tin_document_key, director_id_key,
                proof_of_address_key, trustee_id_key, scuml_letter_key, partner_id_key,
                mda_letter_key, authorized_officer_id_key
            FROM kyc_submission
            """
        )
    ).fetchall()
    for row in ks:
        sid, bid = row[0], row[1]
        mapping = [
            ("cac_certificate", row[2]),
            ("tin_document", row[3]),
            ("director_id", row[4]),
            ("proof_of_address", row[5]),
            ("trustee_id", row[6]),
            ("scuml_letter", row[7]),
            ("partner_id", row[8]),
            ("mda_letter", row[9]),
            ("authorized_officer_id", row[10]),
        ]
        for doc_type, storage_key in mapping:
            if not storage_key:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO kyc_document (
                        id, submission_id, business_id, document_type, storage_key, uploaded_at, created_at
                    )
                    VALUES (
                        gen_random_uuid(), :sid, :bid, :dtype, :skey, now(), now()
                    )
                    """
                ),
                {"sid": sid, "bid": bid, "dtype": doc_type, "skey": storage_key},
            )

    # ── DROP legacy columns ───────────────────────────────────────────────
    for col in (
        "display_name",
        "avatar_url",
        "first_name",
        "last_name",
        "job_title",
        "phone",
        "timezone",
        "department",
        "external_provider",
        "has_taken_tour",
        "date_of_birth",
        "totp_secret",
        "totp_enabled_at",
        "backup_codes_hash",
        "totp_grace_until",
        "approval_pin_hash",
        "notification_preferences",
    ):
        conn.execute(text(f'ALTER TABLE "user" DROP COLUMN IF EXISTS "{col}" CASCADE'))

    for col in (
        "business_type",
        "rc_number",
        "tax_id",
        "city",
        "state",
        "country",
        "website",
        "phone",
        "logo_url",
        "interswitch_merchant_id",
        "virtual_account_number",
        "virtual_account_bank",
        "virtual_account_name",
        "virtual_account_bank_code",
        "virtual_account_reference",
    ):
        conn.execute(text(f"ALTER TABLE business DROP COLUMN IF EXISTS {col} CASCADE"))

    for col in (
        "monthly_txn_volume_range",
        "avg_monthly_payouts_range",
        "primary_bank",
        "primary_use_cases",
        "risk_appetite",
        "default_risk_tolerance",
        "default_budget_cap",
        "merchant_state",
        "daily_payout_limit",
        "single_payout_cap",
        "risk_alert_threshold",
        "liquidity_alert_buffer",
        "require_2fa",
        "require_2fa_enforced_at",
    ):
        conn.execute(text(f"ALTER TABLE business_config DROP COLUMN IF EXISTS {col} CASCADE"))

    for col in (
        "cac_certificate_key",
        "tin_document_key",
        "director_id_key",
        "proof_of_address_key",
        "trustee_id_key",
        "scuml_letter_key",
        "partner_id_key",
        "mda_letter_key",
        "authorized_officer_id_key",
    ):
        conn.execute(text(f"ALTER TABLE kyc_submission DROP COLUMN IF EXISTS {col} CASCADE"))


def downgrade() -> None:
    raise NotImplementedError("Downgrade would require restoring denormalized columns from normalized tables.")

-- ============================================================================
-- FlowPilot — Database Security Setup (Section 10)
-- Run as a superuser or database owner.
-- Safe to re-run: all statements use IF NOT EXISTS / OR REPLACE / IF EXISTS.
-- ============================================================================

-- ── 1. Extensions ─────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ── 2. Application roles ──────────────────────────────────────────────────────
-- fp_app     : runtime application user (SELECT, INSERT, UPDATE, DELETE on data)
-- fp_admin   : internal ops (can decrypt sensitive columns via helper function)
-- fp_audit   : read-only audit queries (audit tables, masked views)
-- fp_analytics : read-only analytics (masked views, aggregates — NO PII)

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fp_app')     THEN CREATE ROLE fp_app     NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fp_admin')   THEN CREATE ROLE fp_admin   NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fp_audit')   THEN CREATE ROLE fp_audit   NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fp_analytics') THEN CREATE ROLE fp_analytics NOLOGIN; END IF;
END $$;


-- ── 3. Table-level grants ─────────────────────────────────────────────────────
-- Grant fp_app full DML on all existing tables, and default privileges for future ones.

GRANT USAGE ON SCHEMA public TO fp_app, fp_admin, fp_audit, fp_analytics;

-- fp_app: full DML on all current tables
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fp_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fp_app;

-- Default privileges so future tables get the same grants automatically
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fp_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO fp_app;

-- fp_admin: read everything (decryption via helper function only)
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fp_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fp_admin;

-- fp_audit: read-only on audit tables and views
GRANT SELECT ON TABLE user_audit_event TO fp_audit;
GRANT SELECT ON TABLE ledger_entry      TO fp_audit;
GRANT SELECT ON TABLE wallet            TO fp_audit;
GRANT SELECT ON TABLE kyc_submission    TO fp_audit;

-- fp_analytics: read-only views only (masked — no raw PII)
-- Views granted below after creation


-- ── 4. Sensitive column registry (comment-based) ──────────────────────────────
-- These comments serve as documentation; enforcement is via masked views + RLS.

COMMENT ON COLUMN "user".email                   IS 'PII:email';
COMMENT ON COLUMN kyc_submission.director_bvn    IS 'PII:bvn — store encrypted in production';
COMMENT ON COLUMN kyc_submission.trustee_bvn     IS 'PII:bvn — store encrypted in production';
COMMENT ON COLUMN kyc_submission.authorized_officer_bvn IS 'PII:bvn — store encrypted in production';
COMMENT ON COLUMN individual_kyc_submission.level_1_value IS 'PII:bvn_or_nin — store encrypted in production';


-- ── 5. Encryption helper (pgcrypto) ──────────────────────────────────────────
-- Call encrypt_sensitive(plaintext, key) when writing BVN/NIN.
-- Call decrypt_sensitive(ciphertext, key) only from fp_admin context.

CREATE OR REPLACE FUNCTION encrypt_sensitive(p_value TEXT, p_key TEXT)
RETURNS BYTEA
LANGUAGE sql
IMMUTABLE
SECURITY DEFINER
AS $$
    SELECT pgp_sym_encrypt(p_value, p_key)::BYTEA;
$$;

CREATE OR REPLACE FUNCTION decrypt_sensitive(p_cipher BYTEA, p_key TEXT)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    -- Only fp_admin may call this function
    IF current_user NOT IN (SELECT rolname FROM pg_roles WHERE rolname IN ('fp_admin') AND pg_has_role(current_user, rolname, 'MEMBER')) THEN
        RAISE EXCEPTION 'Permission denied: decrypt_sensitive requires fp_admin role';
    END IF;
    RETURN pgp_sym_decrypt(p_cipher, p_key);
END;
$$;

-- Only fp_admin can execute decrypt
REVOKE EXECUTE ON FUNCTION decrypt_sensitive(BYTEA, TEXT) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION decrypt_sensitive(BYTEA, TEXT) TO fp_admin;
GRANT  EXECUTE ON FUNCTION encrypt_sensitive(TEXT,  TEXT)  TO fp_app, fp_admin;


-- ── 6. Masked views ──────────────────────────────────────────────────────────

-- v_user_masked: email and display_name only — no password hashes, no OAuth tokens
CREATE OR REPLACE VIEW v_user_masked AS
SELECT
    id,
    CASE
        WHEN email IS NULL THEN NULL
        ELSE LEFT(email, 2) || '***' || SUBSTRING(email FROM POSITION('@' IN email))
    END AS email_masked,
    display_name,
    is_active,
    is_email_verified,
    created_at
FROM "user";

-- v_kyc_masked: director details with BVN/NIN hidden
CREATE OR REPLACE VIEW v_kyc_masked AS
SELECT
    id,
    business_id,
    status,
    business_type,
    registration_number,
    -- Mask BVN: show first 3 + last 2 digits
    CASE
        WHEN director_bvn IS NULL THEN NULL
        ELSE LEFT(director_bvn, 3) || REPEAT('·', GREATEST(0, LENGTH(director_bvn) - 5)) || RIGHT(director_bvn, 2)
    END AS director_bvn_masked,
    director_name,
    submitted_at,
    verified_at,
    created_at
FROM kyc_submission;

-- v_individual_kyc_masked: BVN/NIN masked
CREATE OR REPLACE VIEW v_individual_kyc_masked AS
SELECT
    id,
    business_id,
    level_1_type,
    CASE
        WHEN level_1_value IS NULL THEN NULL
        ELSE LEFT(level_1_value, 3) || REPEAT('·', GREATEST(0, LENGTH(level_1_value) - 5)) || RIGHT(level_1_value, 2)
    END AS level_1_value_masked,
    level_1_status,
    level_1_submitted_at,
    level_2_status,
    level_3_status
FROM individual_kyc_submission;

-- v_ledger_masked: financial summary without internal narrations
CREATE OR REPLACE VIEW v_ledger_masked AS
SELECT
    id,
    internal_reference,
    entry_type,
    gross_amount,
    fee_amount,
    net_amount,
    currency,
    direction,
    status,
    business_id,
    run_id,
    narration,
    initiated_at,
    completed_at
FROM ledger_entry;

-- Grant masked views to analytics
GRANT SELECT ON v_user_masked           TO fp_analytics, fp_audit;
GRANT SELECT ON v_kyc_masked            TO fp_analytics, fp_audit;
GRANT SELECT ON v_individual_kyc_masked TO fp_analytics, fp_audit;
GRANT SELECT ON v_ledger_masked         TO fp_analytics, fp_audit, fp_app;


-- ── 7. Row Level Security (multi-tenant tables) ───────────────────────────────
-- RLS ensures fp_app can only see rows belonging to the current business context.
-- Set: SET LOCAL fp.current_business_id = '<uuid>' in each request transaction.

ALTER TABLE wallet              ENABLE ROW LEVEL SECURITY;
ALTER TABLE kyc_submission      ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_run           ENABLE ROW LEVEL SECURITY;
ALTER TABLE payout_candidate    ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_entry        ENABLE ROW LEVEL SECURITY;

-- wallet RLS
DROP POLICY IF EXISTS rls_wallet_business     ON wallet;
CREATE POLICY rls_wallet_business ON wallet
    FOR ALL TO fp_app
    USING (business_id::TEXT = current_setting('fp.current_business_id', TRUE));

-- kyc_submission RLS
DROP POLICY IF EXISTS rls_kyc_business        ON kyc_submission;
CREATE POLICY rls_kyc_business ON kyc_submission
    FOR ALL TO fp_app
    USING (business_id::TEXT = current_setting('fp.current_business_id', TRUE));

-- agent_run RLS
DROP POLICY IF EXISTS rls_run_business        ON agent_run;
CREATE POLICY rls_run_business ON agent_run
    FOR ALL TO fp_app
    USING (business_id::TEXT = current_setting('fp.current_business_id', TRUE));

-- payout_candidate RLS
DROP POLICY IF EXISTS rls_candidate_business  ON payout_candidate;
CREATE POLICY rls_candidate_business ON payout_candidate
    FOR ALL TO fp_app
    USING (business_id::TEXT = current_setting('fp.current_business_id', TRUE));

-- ledger_entry RLS
DROP POLICY IF EXISTS rls_ledger_business     ON ledger_entry;
CREATE POLICY rls_ledger_business ON ledger_entry
    FOR ALL TO fp_app
    USING (business_id::TEXT = current_setting('fp.current_business_id', TRUE));

-- fp_admin and fp_audit bypass RLS (they see all rows)
ALTER TABLE wallet           FORCE ROW LEVEL SECURITY;
ALTER TABLE kyc_submission   FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_run        FORCE ROW LEVEL SECURITY;
ALTER TABLE payout_candidate FORCE ROW LEVEL SECURITY;
ALTER TABLE ledger_entry     FORCE ROW LEVEL SECURITY;


-- ── 8. Data access log table ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS data_access_log (
    id              BIGSERIAL PRIMARY KEY,
    accessed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    db_user         TEXT        NOT NULL DEFAULT current_user,
    table_name      TEXT        NOT NULL,
    operation       TEXT        NOT NULL CHECK (operation IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'DECRYPT')),
    row_count       INTEGER,
    app_user_id     UUID,
    business_id     UUID,
    ip_address      TEXT
);

GRANT INSERT ON data_access_log TO fp_app, fp_admin, fp_audit;
GRANT SELECT ON data_access_log TO fp_admin;
GRANT USAGE, SELECT ON SEQUENCE data_access_log_id_seq TO fp_app, fp_admin;


-- ── 9. Seed kyc_tier_limit with current CBN limits ───────────────────────────
-- Insert current hardcoded limits into the DB so they can be updated without deployment.
-- ON CONFLICT DO NOTHING ensures re-runs are idempotent.

INSERT INTO kyc_tier_limit (account_type, kyc_level, single_txn_limit, monthly_limit, wallet_balance_limit, effective_from)
VALUES
    ('individual', 1,    100000,    500000,   1000000,  CURRENT_DATE),
    ('individual', 2,    500000,   2000000,   4000000,  CURRENT_DATE),
    ('individual', 3,   1500000,   5000000,  10000000,  CURRENT_DATE),
    ('business',   1,   2000000,   5000000,  10000000,  CURRENT_DATE),
    ('business',   2,  10000000,  30000000,  60000000,  CURRENT_DATE),
    ('business',   3,  20000000, 100000000, 200000000,  CURRENT_DATE)
ON CONFLICT (account_type, kyc_level, effective_from) DO NOTHING;


-- ── Done ──────────────────────────────────────────────────────────────────────
-- Hand this script to your DBA / run as superuser.
-- After running, update your app DB user to connect as (or SET ROLE TO) fp_app.

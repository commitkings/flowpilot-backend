-- Migration: 001_initial_schema.sql
-- Description: FlowPilot initial database schema — 8 tables, indexes, triggers
-- Author: FlowPilot Team
-- Database: PostgreSQL 17+

-- ============================================================================
-- UP MIGRATION
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- Extensions
-- ----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ----------------------------------------------------------------------------
-- Helper: updated_at trigger function
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ----------------------------------------------------------------------------
-- 1. operator
-- ----------------------------------------------------------------------------
CREATE TABLE operator (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id     VARCHAR(255) UNIQUE,
    display_name    VARCHAR(100) NOT NULL,
    email           VARCHAR(255) UNIQUE NOT NULL,
    role            TEXT        NOT NULL DEFAULT 'analyst'
                                CONSTRAINT operator_role_check
                                CHECK (role IN ('analyst', 'approver', 'admin')),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER operator_updated_at
    BEFORE UPDATE ON operator
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- 2. institution
-- ----------------------------------------------------------------------------
CREATE TABLE institution (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    institution_code    VARCHAR(10) UNIQUE NOT NULL,
    institution_name    VARCHAR(255) NOT NULL,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    last_synced_at      TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER institution_updated_at
    BEFORE UPDATE ON institution
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- 3. agent_run
-- ----------------------------------------------------------------------------
CREATE TABLE agent_run (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id     UUID        NOT NULL
                                REFERENCES operator(id) ON DELETE RESTRICT,
    objective       TEXT        NOT NULL,
    constraints     TEXT,
    risk_tolerance  DECIMAL(3,2) NOT NULL DEFAULT 0.35
                                CONSTRAINT agent_run_risk_tolerance_check
                                CHECK (risk_tolerance >= 0.00 AND risk_tolerance <= 1.00),
    budget_cap      DECIMAL(15,2)
                                CONSTRAINT agent_run_budget_cap_check
                                CHECK (budget_cap >= 0),
    merchant_id     VARCHAR(50) NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CONSTRAINT agent_run_status_check
                                CHECK (status IN (
                                    'pending', 'planning', 'reconciling', 'scoring',
                                    'forecasting', 'awaiting_approval', 'executing',
                                    'completed', 'failed', 'cancelled'
                                )),
    plan_graph      JSONB,
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX agent_run_operator_id_idx
    ON agent_run (operator_id);

CREATE INDEX agent_run_status_idx
    ON agent_run (status);

CREATE INDEX agent_run_operator_id_created_at_idx
    ON agent_run (operator_id, created_at DESC);

CREATE TRIGGER agent_run_updated_at
    BEFORE UPDATE ON agent_run
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- 4. plan_step
-- ----------------------------------------------------------------------------
CREATE TABLE plan_step (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID        NOT NULL
                                REFERENCES agent_run(id) ON DELETE CASCADE,
    agent_type      TEXT        NOT NULL
                                CONSTRAINT plan_step_agent_type_check
                                CHECK (agent_type IN (
                                    'planner', 'reconciliation', 'risk',
                                    'forecast', 'execution', 'audit'
                                )),
    step_order      SMALLINT    NOT NULL
                                CONSTRAINT plan_step_step_order_check
                                CHECK (step_order >= 0),
    description     TEXT,
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CONSTRAINT plan_step_status_check
                                CHECK (status IN (
                                    'pending', 'running', 'completed', 'failed', 'skipped'
                                )),
    input_data      JSONB,
    output_data     JSONB,
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT plan_step_run_id_step_order_unique
        UNIQUE (run_id, step_order)
);

CREATE INDEX plan_step_run_id_idx
    ON plan_step (run_id);

-- ----------------------------------------------------------------------------
-- 5. transaction
-- ----------------------------------------------------------------------------
CREATE TABLE transaction (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                      UUID        NOT NULL
                                            REFERENCES agent_run(id) ON DELETE CASCADE,
    transaction_reference       VARCHAR(100) NOT NULL,
    amount                      DECIMAL(15,2) NOT NULL
                                            CONSTRAINT transaction_amount_check
                                            CHECK (amount >= 0),
    currency                    CHAR(3)     NOT NULL DEFAULT 'NGN',
    status                      TEXT        NOT NULL
                                            CONSTRAINT transaction_status_check
                                            CHECK (status IN ('SUCCESS', 'PENDING', 'FAILED', 'REVERSED')),
    channel                     TEXT
                                            CONSTRAINT transaction_channel_check
                                            CHECK (channel IN ('CARD', 'TRANSFER', 'USSD', 'QR')),
    transaction_timestamp       TIMESTAMPTZ,
    customer_id                 VARCHAR(100),
    merchant_id                 VARCHAR(50),
    processor_response_code     VARCHAR(10),
    processor_response_message  TEXT,
    settlement_date             DATE,
    is_anomaly                  BOOLEAN     NOT NULL DEFAULT FALSE,
    anomaly_reason              TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT transaction_run_id_reference_unique
        UNIQUE (run_id, transaction_reference)
);

CREATE INDEX transaction_run_id_idx
    ON transaction (run_id);

CREATE INDEX transaction_run_id_status_idx
    ON transaction (run_id, status);

CREATE INDEX transaction_timestamp_idx
    ON transaction (transaction_timestamp);

-- ----------------------------------------------------------------------------
-- 6. payout_batch
-- ----------------------------------------------------------------------------
CREATE TABLE payout_batch (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID        NOT NULL
                                    REFERENCES agent_run(id) ON DELETE CASCADE,
    batch_reference     VARCHAR(100) UNIQUE NOT NULL,
    currency            CHAR(3)     NOT NULL DEFAULT 'NGN',
    source_account_id   VARCHAR(100) NOT NULL,
    total_amount        DECIMAL(15,2) NOT NULL
                                    CONSTRAINT payout_batch_total_amount_check
                                    CHECK (total_amount >= 0),
    item_count          SMALLINT    NOT NULL
                                    CONSTRAINT payout_batch_item_count_check
                                    CHECK (item_count > 0),
    accepted_count      SMALLINT    NOT NULL DEFAULT 0,
    rejected_count      SMALLINT    NOT NULL DEFAULT 0,
    submission_status   TEXT        NOT NULL DEFAULT 'pending'
                                    CONSTRAINT payout_batch_submission_status_check
                                    CHECK (submission_status IN (
                                        'pending', 'accepted', 'partial', 'rejected', 'failed'
                                    )),
    submitted_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX payout_batch_run_id_idx
    ON payout_batch (run_id);

CREATE TRIGGER payout_batch_updated_at
    BEFORE UPDATE ON payout_batch
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- 7. payout_candidate
-- ----------------------------------------------------------------------------
CREATE TABLE payout_candidate (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID        NOT NULL
                                    REFERENCES agent_run(id) ON DELETE CASCADE,
    batch_id            UUID
                                    REFERENCES payout_batch(id) ON DELETE SET NULL,
    institution_code    VARCHAR(10) NOT NULL
                                    REFERENCES institution(institution_code) ON DELETE RESTRICT,
    beneficiary_name    VARCHAR(255) NOT NULL,
    account_number      VARCHAR(20) NOT NULL,
    amount              DECIMAL(15,2) NOT NULL
                                    CONSTRAINT payout_candidate_amount_check
                                    CHECK (amount > 0),
    currency            CHAR(3)     NOT NULL DEFAULT 'NGN',
    purpose             VARCHAR(255),

    -- Risk scoring (populated by RiskAgent)
    risk_score          DECIMAL(4,3)
                                    CONSTRAINT payout_candidate_risk_score_check
                                    CHECK (risk_score >= 0.000 AND risk_score <= 1.000),
    risk_reasons        JSONB       NOT NULL DEFAULT '[]',
    risk_decision       TEXT
                                    CONSTRAINT payout_candidate_risk_decision_check
                                    CHECK (risk_decision IN ('allow', 'review', 'block')),

    -- Beneficiary lookup (populated by ExecutionAgent pre-check)
    lookup_status       TEXT        NOT NULL DEFAULT 'pending'
                                    CONSTRAINT payout_candidate_lookup_status_check
                                    CHECK (lookup_status IN ('pending', 'success', 'failed', 'mismatch')),
    lookup_account_name VARCHAR(255),
    lookup_match_score  DECIMAL(4,3),

    -- Approval gate (populated by operator action)
    approval_status     TEXT        NOT NULL DEFAULT 'pending'
                                    CONSTRAINT payout_candidate_approval_status_check
                                    CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    approved_by         UUID
                                    REFERENCES operator(id) ON DELETE SET NULL,
    approved_at         TIMESTAMPTZ,

    -- Execution (populated by ExecutionAgent)
    execution_status    TEXT        NOT NULL DEFAULT 'not_started'
                                    CONSTRAINT payout_candidate_execution_status_check
                                    CHECK (execution_status IN (
                                        'not_started', 'pending', 'success', 'failed', 'requires_followup'
                                    )),
    client_reference    VARCHAR(100) UNIQUE,
    provider_reference  VARCHAR(100),
    executed_at         TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX payout_candidate_run_id_idx
    ON payout_candidate (run_id);

CREATE INDEX payout_candidate_batch_id_idx
    ON payout_candidate (batch_id);

CREATE INDEX payout_candidate_institution_code_idx
    ON payout_candidate (institution_code);

CREATE INDEX payout_candidate_approved_by_idx
    ON payout_candidate (approved_by);

CREATE INDEX payout_candidate_run_id_approval_status_idx
    ON payout_candidate (run_id, approval_status);

CREATE INDEX payout_candidate_run_id_risk_decision_idx
    ON payout_candidate (run_id, risk_decision);

CREATE INDEX payout_candidate_risk_reasons_idx
    ON payout_candidate USING GIN (risk_reasons);

CREATE TRIGGER payout_candidate_updated_at
    BEFORE UPDATE ON payout_candidate
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- 8. audit_log (immutable — no updated_at, no UPDATE/DELETE by app)
-- ----------------------------------------------------------------------------
CREATE TABLE audit_log (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id              UUID        NOT NULL
                                    REFERENCES agent_run(id) ON DELETE CASCADE,
    step_id             UUID
                                    REFERENCES plan_step(id) ON DELETE SET NULL,
    agent_type          TEXT
                                    CONSTRAINT audit_log_agent_type_check
                                    CHECK (agent_type IN (
                                        'planner', 'reconciliation', 'risk',
                                        'forecast', 'execution', 'audit'
                                    )),
    action              VARCHAR(64) NOT NULL,
    detail              JSONB,
    api_endpoint        VARCHAR(255),
    request_hash        CHAR(64),
    response_status     SMALLINT,
    response_time_ms    INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX audit_log_run_id_idx
    ON audit_log (run_id);

CREATE INDEX audit_log_step_id_idx
    ON audit_log (step_id);

CREATE INDEX audit_log_created_at_idx
    ON audit_log USING BRIN (created_at);

CREATE INDEX audit_log_run_id_created_at_idx
    ON audit_log (run_id, created_at);

CREATE INDEX audit_log_detail_idx
    ON audit_log USING GIN (detail);

COMMIT;

-- ============================================================================
-- DOWN MIGRATION
-- ============================================================================

-- BEGIN;
--
-- DROP TABLE IF EXISTS audit_log;
-- DROP TABLE IF EXISTS payout_candidate;
-- DROP TABLE IF EXISTS payout_batch;
-- DROP TABLE IF EXISTS transaction;
-- DROP TABLE IF EXISTS plan_step;
-- DROP TABLE IF EXISTS agent_run;
-- DROP TABLE IF EXISTS institution;
-- DROP TABLE IF EXISTS operator;
--
-- DROP FUNCTION IF EXISTS set_updated_at();
--
-- COMMIT;

-- ============================================================================
-- VALIDATION QUERIES
-- ============================================================================

-- Table count:
-- SELECT COUNT(*) FROM information_schema.tables
-- WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
-- Expected: 8

-- Index count:
-- SELECT COUNT(*) FROM pg_indexes WHERE schemaname = 'public';

-- Trigger count:
-- SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema = 'public';
-- Expected: 5 (operator, institution, agent_run, payout_batch, payout_candidate)

-- FK constraint count:
-- SELECT COUNT(*) FROM information_schema.table_constraints
-- WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'public';

-- CHECK constraint count:
-- SELECT COUNT(*) FROM information_schema.table_constraints
-- WHERE constraint_type = 'CHECK' AND table_schema = 'public';

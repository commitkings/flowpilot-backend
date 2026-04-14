-- Migration 004: Wallet system
-- Creates wallet (one per business) and wallet_transaction (immutable ledger) tables.
-- The wallet balance is protected by a CHECK constraint (non-negative) and mutations
-- use SELECT FOR UPDATE row-level locking to prevent race conditions.

-- ── wallet ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS wallet (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL UNIQUE REFERENCES business(id) ON DELETE CASCADE,
    balance     NUMERIC(18, 2)  NOT NULL DEFAULT 0.00,
    currency    CHAR(3)         NOT NULL DEFAULT 'NGN',
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT now(),

    CONSTRAINT wallet_balance_non_negative CHECK (balance >= 0)
);

CREATE INDEX IF NOT EXISTS wallet_business_id_idx ON wallet (business_id);

-- ── wallet_transaction ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS wallet_transaction (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id      UUID        NOT NULL REFERENCES wallet(id) ON DELETE CASCADE,
    business_id    UUID        NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    type           TEXT        NOT NULL,
    amount         NUMERIC(18, 2) NOT NULL,
    -- unique reference enforces idempotency: the same operation cannot be applied twice
    reference      VARCHAR(255) NOT NULL UNIQUE,
    description    TEXT,
    run_id         UUID        REFERENCES agent_run(id) ON DELETE SET NULL,
    balance_before NUMERIC(18, 2) NOT NULL,
    balance_after  NUMERIC(18, 2) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT wallet_tx_amount_positive CHECK (amount > 0),
    CONSTRAINT wallet_tx_type_check      CHECK (type IN ('credit', 'debit'))
);

CREATE INDEX IF NOT EXISTS wallet_tx_wallet_id_idx   ON wallet_transaction (wallet_id);
CREATE INDEX IF NOT EXISTS wallet_tx_business_id_idx ON wallet_transaction (business_id);
CREATE UNIQUE INDEX IF NOT EXISTS wallet_tx_reference_idx ON wallet_transaction (reference);
CREATE INDEX IF NOT EXISTS wallet_tx_run_id_idx      ON wallet_transaction (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS wallet_tx_created_at_idx  ON wallet_transaction (created_at DESC);

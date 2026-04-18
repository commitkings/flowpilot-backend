# FlowPilot — Schema Redesign & Payee Self-Service Portal
## Architecture, Normalization & Implementation Plan

**Version:** 1.0  
**Date:** April 2026  
**Regulatory Frameworks:** CBN AML/CFT Guidelines 2023, NDPC Act 2023, FATF Recommendation 16 (Travel Rule), ISO 20022

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current Schema Problems](#2-current-schema-problems)
3. [Normalization Principles Applied](#3-normalization-principles-applied)
4. [Nigerian Regulatory Requirements](#4-nigerian-regulatory-requirements)
5. [Normalized Schema — Full Design](#5-normalized-schema--full-design)
6. [Payee Self-Service Portal — Full Design](#6-payee-self-service-portal--full-design)
7. [Implementation Phases](#7-implementation-phases)
8. [Migration Strategy](#8-migration-strategy)
9. [Financial Security & Integrity Gaps](#9-financial-security--integrity-gaps)

---

## 1. Executive Summary

The current FlowPilot schema has several structural problems that create audit risk, compliance exposure, and technical debt. Specifically:

- **Tables with 25–30+ columns** that mix concerns (auth + profile, config + policy, identity + document)
- **Repeated data groups** in single rows (KYC levels 1/2/3 as column groups violates 1NF)
- **Denormalized beneficiary data** copied into at least 5 tables (`payout_candidate`, `beneficiary_reputation`, `run_outcome_memory`, `payout_compliance_record`, `customer_lookup_result`)
- **JSONB fields used for structured data** that should be relational tables (`notification_preferences`, `primary_use_cases`, `partner_names`)
- **No complete general-purpose audit event table** — the existing `audit_log` only covers agent actions, not user actions (login, config changes, team changes), which is a CBN AML requirement
- **Virtual account fields embedded on `business`** rather than a dedicated table — limits future multi-account support
- **No `platform_fee_transaction` ledger** — fees are stored only on `agent_run`, not in an immutable ledger as required by financial accounting standards
- **No central transaction ledger** — money movements are split across 6 tables with no unified view; a CBN auditor asking "show me every naira this business moved" requires a UNION across `wallet_transaction`, `payout_candidate`, `payout_execution`, `reconciled_transaction`, `platform_fee_transaction`, and `ai_credit_transaction` — which is unacceptable for a regulated financial platform

This document redesigns every affected table to 3NF (Third Normal Form), adds the missing compliance infrastructure, and introduces the Payee Self-Service Portal as a set of new, properly normalized tables.

---

## 2. Current Schema Problems

### 2.1 `user` table (24 columns — should be 10)

| Problem | Columns Affected | Fix |
|---|---|---|
| Auth mixed with profile | `first_name`, `last_name`, `job_title`, `phone`, `timezone`, `department`, `avatar_url`, `date_of_birth` | Extract to `user_profile` |
| 2FA/MFA mixed with identity | `totp_secret`, `totp_enabled_at`, `backup_codes_hash`, `totp_grace_until`, `approval_pin_hash` | Extract to `user_mfa` |
| Single OAuth provider | `external_id`, `external_provider` | Extract to `user_oauth_provider` (supports Google + future providers) |
| JSONB for structured prefs | `notification_preferences` | Extract to `user_notification_preference` (queryable, indexable) |
| `has_taken_tour` on identity table | `has_taken_tour` | Move to `user_profile` or `user_onboarding_state` |

### 2.2 `business` table (22 columns — should be 8)

| Problem | Columns Affected | Fix |
|---|---|---|
| Contact/address embedded | `city`, `state`, `country`, `website`, `phone`, `logo_url` | Extract to `business_profile` |
| 5 virtual account fields | `virtual_account_number`, `virtual_account_bank`, `virtual_account_name`, `virtual_account_bank_code`, `virtual_account_reference` | Extract to `business_virtual_account` |
| Mutable counter on reference table | `ai_credit_balance` | Remove — derive from `ai_credit_transaction` SUM |
| KYC status stored redundantly | `kyc_status`, `kyc_level` | Derive from `kyc_submission`/`kyc_verification_level` OR keep as a denormalized cache with clear ownership |

### 2.3 `business_config` table (20 columns — should be split into 3)

| Problem | Columns Affected | Fix |
|---|---|---|
| Financial policy mixed with onboarding | `daily_payout_limit`, `single_payout_cap`, `default_budget_cap`, `default_risk_tolerance`, `risk_alert_threshold`, `liquidity_alert_buffer` | Extract to `business_payment_policy` |
| 2FA enforcement mixed with config | `require_2fa`, `require_2fa_enforced_at` | Extract to `business_security_policy` |
| Use cases as JSONB array | `primary_use_cases` | Extract to `business_use_case` |
| Onboarding state in config | `onboarding_step`, `onboarding_completed_at` | Keep in `business_config` (acceptable as onboarding metadata) |

### 2.4 `kyc_submission` table (25 columns — critical violation)

This table contains fields for 5 different business types in the same row, with most columns NULL for any given submission. This is a classic **partial dependency** violation.

| Problem | Example | Fix |
|---|---|---|
| 5 entity types share one row | `director_name` is NULL for NGOs; `trustee_name` is NULL for LLCs | Create `kyc_principal` table (one row per person — director/trustee/partner/officer) |
| Document keys embedded in row | 9 document key columns, most NULL | Create `kyc_document` table (one row per document) |
| Partner names as JSON string in Text | `partner_names TEXT` | Replace with `kyc_principal` rows with `role = 'partner'` |
| No history — single-row upsert | Re-submission overwrites previous | Add `kyc_submission_id` FK so history is preserved |

### 2.5 `individual_kyc_submission` table (15 columns — 1NF violation)

The three KYC levels are stored as repeating column groups: `level_1_type`, `level_1_value`, `level_1_status`, `level_1_submitted_at`, `level_1_verified_at` — then repeated for level 2 and 3. This is a textbook First Normal Form violation.

**Fix:** Replace with `kyc_verification_level` table — one row per level per business.

### 2.6 `payout_candidate` table (28 columns — mixed concerns)

| Problem | Columns Affected | Fix |
|---|---|---|
| Beneficiary identity repeated | `beneficiary_name`, `account_number`, `institution_code`, `beneficiary_email` | FK to `payee_bank_account` |
| Execution state on candidate | `provider`, `provider_status`, `monnify_reference`, `monnify_status`, `client_reference`, `provider_reference`, `transaction_reference`, `executed_at` | These belong in `payout_execution` (already exists) |
| Lookup results on candidate | `lookup_status`, `lookup_account_name`, `lookup_match_score` | Already in `customer_lookup_result` — remove from candidate (keep as last-known-value cache only) |

### 2.7 Denormalized beneficiary identity across 5 tables

`account_number` + `bank_code` + `beneficiary_name` appears in:
- `payout_candidate`
- `beneficiary_reputation`
- `run_outcome_memory`
- `payout_compliance_record`
- `customer_lookup_result`

**Fix:** Introduce `payee_bank_account` as the single source of truth. All other tables FK to it.

### 2.8 Missing compliance tables

| Missing Table | Why It's Required |
|---|---|
| `user_audit_event` | CBN AML/CFT requires all user actions logged (login, config change, team change, approval) — separate from agent audit |
| `ledger_entry` | Central transaction ledger — every naira movement in one place with full sender, receiver, narration, and reference detail. Required for CBN audit, financial reporting, and reconciliation |
| `platform_fee_transaction` | Financial accounting requires an immutable fee ledger (fees stored only on `agent_run` is insufficient) |
| `data_processing_record` | NDPC Act 2023 requires a Record of Processing Activities (ROPA) |
| `consent_record` | NDPC requires documented user consent for each processing purpose |

---

## 3. Normalization Principles Applied

All tables in the redesign follow **Third Normal Form (3NF)**:

- **1NF**: No repeating groups (KYC level columns replaced with rows)
- **2NF**: No partial dependencies (all non-key columns fully dependent on the whole PK)
- **3NF**: No transitive dependencies (profile data separated from auth data)

Additionally, financial tables follow **immutability rules**:
- Ledger tables (`ledger_entry`, `wallet_transaction`, `platform_fee_transaction`, `ai_credit_transaction`) are append-only — no UPDATE, no DELETE
- Status/state tables use explicit state machines with `CHECK` constraints
- All money columns use `NUMERIC(18, 2)` — never FLOAT

---

## 4. Nigerian Regulatory Requirements

### 4.1 CBN AML/CFT Guidelines (2023)

| Requirement | Implementation |
|---|---|
| Customer Due Diligence (CDD) | `kyc_submission` + `kyc_principal` + `kyc_document` + `kyc_verification_level` |
| Enhanced Due Diligence for high-risk | `business_risk_classification` table with rating + rationale |
| Transaction monitoring | `transaction_monitoring_flag` table linked to `payout_candidate` |
| STR/SAR filing capability | `suspicious_activity_report` table |
| Record retention (5 years minimum) | All tables use `created_at` with BRIN indexes; no hard-delete policy enforced at DB level |
| FATF Travel Rule (≥₦1M transfers) | `payout_travel_rule_record` — already partially covered by `payout_compliance_record` |
| Source of funds documentation | `agent_run.objective` captures this; formalized in `run_source_of_funds` |

### 4.2 NDPC Act 2023

| Requirement | Implementation |
|---|---|
| Lawful basis for processing | `consent_record` table — records consent timestamp, purpose, version of privacy policy |
| Data subject rights (access, erasure) | Soft-delete pattern on `user` and `payee_profile`; `data_subject_request` table tracks requests |
| Record of Processing Activities (ROPA) | `data_processing_record` — documents what data, for what purpose, retained how long |
| Data breach notification | `security_incident` table — records incidents, notified parties, timeline |
| Cross-border transfer restriction | `payee_bank_account.country_code` enforced; transfers outside Nigeria require documented exemption |

### 4.3 CBN Tiered KYC Limits (2023 Circular)

| KYC Level | Max Single Txn | Max Monthly | Max Wallet Balance |
|---|---|---|---|
| Level 0 (no KYC) | ₦0 | ₦0 | ₦0 |
| Level 1 (BVN/NIN) | ₦300,000 | ₦1,500,000 | ₦3,000,000 |
| Level 2 (+ Address) | ₦1,000,000 | ₦10,000,000 | ₦10,000,000 |
| Level 3 (+ Govt ID) | ₦5,000,000 | ₦50,000,000 | ₦50,000,000 |

These limits are stored in code (`kyc_limits.py`) but also need a `kyc_tier_limit` table so limits can be updated by admin without a deployment.

### 4.4 ISO 20022 Alignment

For interoperability with NIP/NIBSS and future international transfers, transaction records should carry ISO 20022-compatible fields:
- `end_to_end_id` on `payout_candidate` (already covered by `client_reference`)
- `transaction_purpose_code` using ISO category codes (e.g., `SALA` for salary, `SUPP` for supplier payment)
- `creditor_agent_bic` on `payout_batch` for international routing

---

## 5. Normalized Schema — Full Design

### 5.1 Auth & Identity Domain

#### `user` (reformed — 10 columns)
```sql
CREATE TABLE "user" (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(255),
    account_type    VARCHAR(20) NOT NULL DEFAULT 'payer'
                    CHECK (account_type IN ('payer', 'payee', 'admin')),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    email_verified_at TIMESTAMPTZ,
    last_login_at   TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

> **Note:** `account_type` distinguishes payer users (business owners/members) from payee users (recipients). This is the key field for the Payee Portal.

---

#### `user_profile` (new — extracted from `user`)
```sql
CREATE TABLE user_profile (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL UNIQUE REFERENCES "user"(id) ON DELETE CASCADE,
    display_name    VARCHAR(100) NOT NULL,
    first_name      VARCHAR(100),
    last_name       VARCHAR(100),
    phone           VARCHAR(30),
    avatar_url      VARCHAR(512),
    date_of_birth   DATE,
    job_title       VARCHAR(150),
    department      VARCHAR(100),
    timezone        VARCHAR(60) DEFAULT 'Africa/Lagos',
    has_taken_tour  BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `user_mfa` (new — extracted from `user`)
```sql
CREATE TABLE user_mfa (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL UNIQUE REFERENCES "user"(id) ON DELETE CASCADE,
    totp_secret         VARCHAR(64),               -- encrypted at app layer
    totp_enabled_at     TIMESTAMPTZ,
    backup_codes_hash   TEXT,                      -- JSON array of bcrypt hashes
    totp_grace_until    TIMESTAMPTZ,
    approval_pin_hash   VARCHAR(255),              -- bcrypt hash of 4-6 digit PIN
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `user_oauth_provider` (new — replaces `external_id`/`external_provider` on `user`)
```sql
CREATE TABLE user_oauth_provider (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    provider        VARCHAR(50) NOT NULL,          -- 'google', 'microsoft', etc.
    external_id     VARCHAR(255) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, external_id)
);
CREATE INDEX user_oauth_provider_user_id_idx ON user_oauth_provider(user_id);
```

---

#### `user_notification_preference` (new — replaces JSONB on `user`)
```sql
CREATE TABLE user_notification_preference (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    channel         VARCHAR(20) NOT NULL CHECK (channel IN ('email', 'in_app', 'whatsapp')),
    event_type      VARCHAR(64) NOT NULL,
    -- e.g. 'kyc_updates', 'login_alerts', 'payment_notifications', 'security_alerts'
    is_enabled      BOOLEAN NOT NULL DEFAULT true,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, channel, event_type)
);
```

---

#### `user_audit_event` (new — CBN AML requirement)
```sql
CREATE TABLE user_audit_event (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         UUID REFERENCES "user"(id) ON DELETE SET NULL,
    business_id     UUID REFERENCES business(id) ON DELETE SET NULL,
    event_type      VARCHAR(64) NOT NULL,
    -- e.g. 'login', 'logout', 'password_change', 'team_invite', 'member_removed',
    --      'kyc_submitted', 'run_created', 'run_approved', 'config_updated',
    --      'api_key_created', 'api_key_revoked'
    resource_type   VARCHAR(64),
    resource_id     UUID,
    ip_address      INET,
    user_agent      TEXT,
    metadata        JSONB,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX user_audit_event_user_id_idx ON user_audit_event(user_id);
CREATE INDEX user_audit_event_business_id_idx ON user_audit_event(business_id);
CREATE INDEX user_audit_event_occurred_at_idx ON user_audit_event USING BRIN(occurred_at);
CREATE INDEX user_audit_event_event_type_idx ON user_audit_event(event_type);
```

> This table is **append-only**. No UPDATE or DELETE. CBN requires 5-year retention.

---

### 5.2 Business Domain

#### `business` (reformed — 8 columns)
```sql
CREATE TABLE business (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_name   VARCHAR(255) NOT NULL,
    account_type    VARCHAR(20) NOT NULL DEFAULT 'business'
                    CHECK (account_type IN ('individual', 'business')),
    kyc_status      VARCHAR(20) NOT NULL DEFAULT 'not_submitted'
                    CHECK (kyc_status IN ('not_submitted', 'pending', 'verified', 'rejected')),
    kyc_level       SMALLINT NOT NULL DEFAULT 0,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `business_profile` (new — extracted from `business`)
```sql
CREATE TABLE business_profile (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id             UUID NOT NULL UNIQUE REFERENCES business(id) ON DELETE CASCADE,
    business_type           VARCHAR(50),
    -- 'limited_company' | 'ngo' | 'sole_proprietorship' | 'partnership' | 'mda'
    rc_number               VARCHAR(50),
    tax_id                  VARCHAR(50),
    phone                   VARCHAR(30),
    website                 VARCHAR(255),
    logo_url                VARCHAR(512),
    interswitch_merchant_id VARCHAR(128),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `business_address` (new — clean separation of location data)
```sql
CREATE TABLE business_address (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     UUID NOT NULL UNIQUE REFERENCES business(id) ON DELETE CASCADE,
    street_line_1   VARCHAR(255),
    street_line_2   VARCHAR(255),
    city            VARCHAR(100),
    state           VARCHAR(100),
    country         VARCHAR(100) NOT NULL DEFAULT 'Nigeria',
    postal_code     VARCHAR(20),
    is_verified     BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `business_virtual_account` (new — extracted from `business`)
```sql
CREATE TABLE business_virtual_account (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id             UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    account_number          VARCHAR(20) NOT NULL,
    account_name            VARCHAR(128),
    bank_name               VARCHAR(128),
    bank_code               VARCHAR(10),
    account_reference       VARCHAR(100) UNIQUE,
    provider                VARCHAR(50) NOT NULL DEFAULT 'monnify',
    -- 'monnify' | 'flutterwave' | 'interswitch'
    is_primary              BOOLEAN NOT NULL DEFAULT true,
    is_active               BOOLEAN NOT NULL DEFAULT true,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, account_number)
);
CREATE INDEX biz_virtual_account_business_id_idx ON business_virtual_account(business_id);
```

---

#### `business_config` (reformed — 8 columns, onboarding only)
```sql
CREATE TABLE business_config (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id             UUID NOT NULL UNIQUE REFERENCES business(id) ON DELETE CASCADE,
    onboarding_step         TEXT NOT NULL DEFAULT 'not_started'
                            CHECK (onboarding_step IN (
                                'not_started', 'business_profile',
                                'financial_setup', 'team_invite', 'complete'
                            )),
    onboarding_completed_at TIMESTAMPTZ,
    preferences             JSONB NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `business_payment_policy` (new — extracted from `business_config`)
```sql
CREATE TABLE business_payment_policy (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id             UUID NOT NULL UNIQUE REFERENCES business(id) ON DELETE CASCADE,
    monthly_txn_volume_range VARCHAR(50),
    avg_monthly_payouts_range VARCHAR(50),
    primary_bank            VARCHAR(100),
    risk_appetite           TEXT CHECK (risk_appetite IN ('conservative', 'moderate', 'aggressive')),
    default_risk_tolerance  NUMERIC(5,4) NOT NULL DEFAULT 0.3500,
    default_budget_cap      NUMERIC(18,2),
    daily_payout_limit      NUMERIC(18,2),
    single_payout_cap       NUMERIC(18,2),
    risk_alert_threshold    NUMERIC(5,4),
    liquidity_alert_buffer  NUMERIC(5,2),
    merchant_state          VARCHAR(100),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT policy_risk_tolerance_check
        CHECK (default_risk_tolerance >= 0 AND default_risk_tolerance <= 1)
);
```

---

#### `business_security_policy` (new — extracted from `business_config`)
```sql
CREATE TABLE business_security_policy (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id             UUID NOT NULL UNIQUE REFERENCES business(id) ON DELETE CASCADE,
    require_2fa             BOOLEAN NOT NULL DEFAULT false,
    require_2fa_enforced_at TIMESTAMPTZ,
    session_timeout_minutes INTEGER NOT NULL DEFAULT 480,
    ip_allowlist            JSONB,                 -- optional IP whitelist for API keys
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `business_use_case` (new — replaces JSONB array on `business_config`)
```sql
CREATE TABLE business_use_case (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    use_case        VARCHAR(64) NOT NULL,
    -- 'salary', 'vendor_payment', 'contractor', 'ngo_disbursement', 'commission', 'other'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (business_id, use_case)
);
```

---

### 5.3 KYC Domain (Fully Normalized)

#### `kyc_submission` (reformed — header only)
```sql
CREATE TABLE kyc_submission (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    submission_type VARCHAR(20) NOT NULL
                    CHECK (submission_type IN ('business', 'individual')),
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'under_review', 'verified', 'rejected')),
    business_type   VARCHAR(50),
    -- Only for business submissions: 'limited_company' | 'ngo' | etc.
    registration_number VARCHAR(100),
    tin_number      VARCHAR(50),
    rejection_reason TEXT,
    submitted_at    TIMESTAMPTZ,
    reviewed_at     TIMESTAMPTZ,
    reviewed_by     UUID REFERENCES "user"(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX kyc_submission_business_id_idx ON kyc_submission(business_id);
CREATE INDEX kyc_submission_status_idx ON kyc_submission(status);
```

---

#### `kyc_document` (new — replaces all `_key` columns)
```sql
CREATE TABLE kyc_document (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id   UUID NOT NULL REFERENCES kyc_submission(id) ON DELETE CASCADE,
    business_id     UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    document_type   VARCHAR(64) NOT NULL,
    -- 'cac_certificate' | 'tin_document' | 'director_id' | 'trustee_id'
    -- 'partner_id' | 'scuml_letter' | 'mda_letter' | 'authorized_officer_id'
    -- 'proof_of_address' | 'government_id' | 'liveness_selfie'
    storage_key     VARCHAR(512) NOT NULL,          -- MinIO object key
    file_name       VARCHAR(255),
    mime_type       VARCHAR(100),
    file_size_bytes INTEGER,
    is_current      BOOLEAN NOT NULL DEFAULT true,  -- false when superseded
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX kyc_document_submission_id_idx ON kyc_document(submission_id);
CREATE INDEX kyc_document_business_id_idx ON kyc_document(business_id);
CREATE INDEX kyc_document_type_idx ON kyc_document(document_type);
```

---

#### `kyc_principal` (new — replaces type-specific name/BVN columns)
```sql
CREATE TABLE kyc_principal (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id   UUID NOT NULL REFERENCES kyc_submission(id) ON DELETE CASCADE,
    business_id     UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    role            VARCHAR(50) NOT NULL,
    -- 'director' | 'trustee' | 'partner' | 'authorized_officer' | 'shareholder'
    full_name       VARCHAR(255) NOT NULL,
    bvn             VARCHAR(20),                    -- encrypted at app layer
    id_document_key VARCHAR(512),                  -- FK to kyc_document in practice
    scuml_number    VARCHAR(100),                   -- NGOs only
    is_primary      BOOLEAN NOT NULL DEFAULT false, -- primary contact for the entity
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX kyc_principal_submission_id_idx ON kyc_principal(submission_id);
```

---

#### `kyc_verification_level` (new — replaces `individual_kyc_submission` repeating groups)
```sql
CREATE TABLE kyc_verification_level (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    submission_id   UUID NOT NULL REFERENCES kyc_submission(id) ON DELETE CASCADE,
    level           SMALLINT NOT NULL CHECK (level IN (1, 2, 3)),
    -- Level 1: BVN/NIN identity
    -- Level 2: Proof of address
    -- Level 3: Government ID + liveness selfie
    id_type         VARCHAR(10) CHECK (id_type IN ('nin', 'bvn')),
    -- Level 1 only
    id_value        VARCHAR(20),
    -- Level 1 only — encrypted at app layer
    address         TEXT,
    -- Level 2 only
    status          VARCHAR(20) NOT NULL DEFAULT 'not_submitted'
                    CHECK (status IN ('not_submitted', 'pending', 'verified', 'rejected')),
    rejection_reason TEXT,
    submitted_at    TIMESTAMPTZ,
    verified_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (business_id, level)
);
CREATE INDEX kyc_verification_level_business_id_idx ON kyc_verification_level(business_id);
CREATE INDEX kyc_verification_level_submission_id_idx ON kyc_verification_level(submission_id);
```

---

#### `kyc_tier_limit` (new — replaces hardcoded `kyc_limits.py`)
```sql
CREATE TABLE kyc_tier_limit (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_type        VARCHAR(20) NOT NULL CHECK (account_type IN ('individual', 'business')),
    kyc_level           SMALLINT NOT NULL,
    single_txn_limit    NUMERIC(18,2) NOT NULL,
    monthly_limit       NUMERIC(18,2) NOT NULL,
    wallet_balance_limit NUMERIC(18,2) NOT NULL,
    effective_from      DATE NOT NULL DEFAULT CURRENT_DATE,
    effective_to        DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_type, kyc_level, effective_from)
);
```

---

### 5.4 Payments Domain

#### `wallet` (reformed — adds `reserved_balance`)

The current wallet has one balance column. The reserve-then-settle model requires two:

```sql
ALTER TABLE wallet
    ADD COLUMN reserved_balance NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    ADD CONSTRAINT wallet_reserved_non_negative CHECK (reserved_balance >= 0),
    ADD CONSTRAINT wallet_reserved_lte_balance  CHECK (reserved_balance <= balance);

-- available_balance is always derived, never stored:
-- available_balance = balance - reserved_balance
-- This is the only amount a business can spend on a new run.
```

Full reformed `wallet` table:

```sql
CREATE TABLE wallet (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL UNIQUE REFERENCES business(id) ON DELETE CASCADE,
    balance             NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    -- Total funds held — includes reserved funds
    reserved_balance    NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    -- Portion of balance locked against in-progress runs
    -- available_balance = balance - reserved_balance (computed at query time)
    currency            CHAR(3) NOT NULL DEFAULT 'NGN',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT wallet_balance_non_negative      CHECK (balance >= 0),
    CONSTRAINT wallet_reserved_non_negative     CHECK (reserved_balance >= 0),
    CONSTRAINT wallet_reserved_lte_balance      CHECK (reserved_balance <= balance),
    CONSTRAINT wallet_balance_integrity         CHECK (balance >= reserved_balance)
);
```

---

#### `wallet_reservation` (new — tracks each run's reserved amount)

One row per run, so the wallet repository always knows exactly what to release or settle per run without scanning transaction history.

```sql
CREATE TABLE wallet_reservation (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id           UUID NOT NULL REFERENCES wallet(id) ON DELETE CASCADE,
    business_id         UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    run_id              UUID NOT NULL UNIQUE REFERENCES agent_run(id) ON DELETE CASCADE,
    reserved_amount     NUMERIC(18,2) NOT NULL CHECK (reserved_amount > 0),
    -- Full amount reserved at approval time (gross — before knowing which fail)
    settled_amount      NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    -- Filled in at settlement — sum of successful payout amounts + fees
    released_amount     NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    -- Filled in at settlement — sum of failed payout amounts returned
    status              VARCHAR(20) NOT NULL DEFAULT 'active'
                        CHECK (status IN (
                            'active',     -- funds locked, run in progress
                            'settled',    -- run complete, balance adjusted
                            'cancelled'   -- run cancelled before execution
                        )),
    reserved_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at          TIMESTAMPTZ,
    ledger_reserve_id   BIGINT REFERENCES ledger_entry(id),
    -- The ledger_entry row written when funds were reserved
    ledger_settle_id    BIGINT REFERENCES ledger_entry(id),
    -- The ledger_entry row written at settlement
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX wallet_reservation_business_id_idx ON wallet_reservation(business_id);
CREATE INDEX wallet_reservation_status_idx      ON wallet_reservation(status);
```

---

#### Reserve-then-settle flow (full specification)

**Step 1 — Reserve at run approval**

Triggered when an approver clicks "Approve" or a run passes automatic approval.

```python
async def reserve_for_run(run_id, business_id, total_amount, session):
    wallet = await _get_locked(business_id, session)  # SELECT FOR UPDATE

    available = wallet.balance - wallet.reserved_balance
    if available < total_amount:
        raise InsufficientBalanceError(
            available=available, required=total_amount
        )

    # Lock the funds
    wallet.reserved_balance += total_amount
    wallet.updated_at = now()

    # Write ledger entry
    entry = LedgerEntry(
        entry_type    = 'wallet_reserve',
        direction     = 'debit',
        gross_amount  = total_amount,
        status        = 'completed',
        business_id   = business_id,
        run_id        = run_id,
        narration     = f"Funds reserved for run #{short_id(run_id)}",
        internal_ref  = generate_ref('WRS'),
    )
    session.add(entry)
    await session.flush()

    # Write reservation record
    reservation = WalletReservation(
        wallet_id        = wallet.id,
        business_id      = business_id,
        run_id           = run_id,
        reserved_amount  = total_amount,
        status           = 'active',
        ledger_reserve_id = entry.id,
    )
    session.add(reservation)
    await session.flush()
```

---

**Step 2 — Execute payouts**

No wallet interaction during execution. The Interswitch/Monnify calls happen. Each candidate is marked `success` or `failed` as results come in.

---

**Step 3 — Settle after execution completes**

Triggered by the audit agent at run close, after all candidates have a terminal status.

```python
async def settle_run(run_id, business_id, session):
    # Sum outcomes from payout_candidate
    successful_amount = SUM(amount WHERE execution_status='success')
    failed_amount     = SUM(amount WHERE execution_status IN ('failed','reversed'))
    successful_fee    = SUM(fee WHERE execution_status='success')
    # failed candidates owe no fee

    wallet     = await _get_locked(business_id, session)  # SELECT FOR UPDATE
    reservation = await get_reservation(run_id, session)

    # 1 — Settle: convert reservation to actual debit (successful amount only)
    wallet.balance          -= (successful_amount + successful_fee)
    wallet.reserved_balance -= reservation.reserved_amount
    # reserved_balance goes down by the FULL reserved amount
    # balance goes down by only the SUCCESSFUL amount + fee
    # the difference is effectively released back to available

    # 2 — Write settle ledger entry
    settle_entry = LedgerEntry(
        entry_type   = 'wallet_settle',
        direction    = 'debit',
        gross_amount = successful_amount,
        fee_amount   = successful_fee,
        net_amount   = successful_amount + successful_fee,
        status       = 'completed',
        run_id       = run_id,
        narration    = f"Settlement for run #{short_id(run_id)} — "
                       f"{n} of {total} payouts successful",
        internal_ref = generate_ref('WST'),
    )
    session.add(settle_entry)

    # 3 — Write release ledger entry (if any failed)
    if failed_amount > 0:
        release_entry = LedgerEntry(
            entry_type   = 'wallet_release',
            direction    = 'credit',
            gross_amount = failed_amount,
            status       = 'completed',
            run_id       = run_id,
            narration    = f"Release — {n_failed} failed payouts returned",
            internal_ref = generate_ref('WRL'),
        )
        session.add(release_entry)

    # 4 — Mark reservation settled
    reservation.settled_amount  = successful_amount + successful_fee
    reservation.released_amount = failed_amount
    reservation.status          = 'settled'
    reservation.settled_at      = now()
    reservation.ledger_settle_id = settle_entry.id

    await session.flush()
```

---

**Step 4 — Run cancelled before execution**

If a run is cancelled after approval but before any payouts execute:

```python
async def cancel_reservation(run_id, business_id, session):
    wallet      = await _get_locked(business_id, session)
    reservation = await get_reservation(run_id, session)

    wallet.reserved_balance -= reservation.reserved_amount
    reservation.status       = 'cancelled'

    session.add(LedgerEntry(
        entry_type   = 'wallet_release',
        direction    = 'credit',
        gross_amount = reservation.reserved_amount,
        status       = 'completed',
        run_id       = run_id,
        narration    = f"Reservation cancelled — run #{short_id(run_id)} cancelled",
        internal_ref = generate_ref('WRL'),
    ))
```

---

**Balance view for the business dashboard:**

```sql
SELECT
    balance                            AS total_balance,
    reserved_balance                   AS locked_in_active_runs,
    balance - reserved_balance         AS available_to_spend
FROM wallet
WHERE business_id = $1;
```

This is what the wallet UI should always display — three distinct numbers so the business always understands exactly where their money is.

---

#### `payee_bank_account` (new — single source of truth for beneficiary identity)

This is the most critical normalization fix. All five tables that currently store raw `account_number + bank_code + name` will FK to this instead.

```sql
CREATE TABLE payee_bank_account (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_number  VARCHAR(20) NOT NULL,
    institution_code VARCHAR(10) NOT NULL REFERENCES institution(institution_code),
    account_name    VARCHAR(255),          -- name returned from BAV
    is_bav_verified BOOLEAN NOT NULL DEFAULT false,
    bav_verified_at TIMESTAMPTZ,
    bav_match_score NUMERIC(5,4),
    -- Optional link to a payee portal account
    payee_profile_id UUID REFERENCES payee_profile(id) ON DELETE SET NULL,
    country_code    CHAR(2) NOT NULL DEFAULT 'NG',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_number, institution_code)
);
CREATE INDEX payee_bank_account_account_number_idx ON payee_bank_account(account_number);
CREATE INDEX payee_bank_account_payee_profile_idx ON payee_bank_account(payee_profile_id);
```

---

#### `payout_candidate` (reformed — 18 columns, down from 28)
```sql
CREATE TABLE payout_candidate (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL REFERENCES agent_run(id) ON DELETE CASCADE,
    business_id         UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    batch_id            UUID REFERENCES payout_batch(id) ON DELETE SET NULL,
    bank_account_id     UUID NOT NULL REFERENCES payee_bank_account(id),
    -- Replaces raw beneficiary_name, account_number, institution_code, beneficiary_email
    amount              NUMERIC(18,2) NOT NULL CHECK (amount > 0),
    currency            CHAR(3) NOT NULL DEFAULT 'NGN',
    purpose             VARCHAR(255),
    purpose_code        VARCHAR(10),               -- ISO 20022 purpose code e.g. 'SALA'
    client_reference    VARCHAR(100) UNIQUE,        -- end-to-end ID (ISO 20022)
    risk_score          NUMERIC(5,4)
                        CHECK (risk_score >= 0 AND risk_score <= 1),
    risk_reasons        JSONB NOT NULL DEFAULT '[]',
    risk_decision       TEXT CHECK (risk_decision IN ('allow', 'review', 'block')),
    approval_status     TEXT NOT NULL DEFAULT 'pending'
                        CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    approved_by         UUID REFERENCES "user"(id) ON DELETE SET NULL,
    approved_at         TIMESTAMPTZ,
    execution_status    TEXT NOT NULL DEFAULT 'not_started'
                        CHECK (execution_status IN (
                            'not_started', 'pending', 'success', 'failed', 'requires_followup'
                        )),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `platform_fee_transaction` (new — immutable fee ledger)
```sql
CREATE TABLE platform_fee_transaction (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES agent_run(id) ON DELETE RESTRICT,
    business_id     UUID NOT NULL REFERENCES business(id) ON DELETE RESTRICT,
    fee_type        VARCHAR(50) NOT NULL DEFAULT 'platform_fee',
    -- 'platform_fee' | 'refund'
    rate            NUMERIC(6,4) NOT NULL,          -- e.g. 0.0060 = 0.6%
    payout_amount   NUMERIC(18,2) NOT NULL,          -- gross payout amount fee was calculated on
    fee_amount      NUMERIC(18,2) NOT NULL,          -- actual fee charged
    min_fee_applied BOOLEAN NOT NULL DEFAULT false,  -- true if ₦50 minimum was applied
    currency        CHAR(3) NOT NULL DEFAULT 'NGN',
    wallet_tx_id    UUID REFERENCES wallet_transaction(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX platform_fee_tx_run_id_idx ON platform_fee_transaction(run_id);
CREATE INDEX platform_fee_tx_business_id_idx ON platform_fee_transaction(business_id);
CREATE INDEX platform_fee_tx_created_at_idx ON platform_fee_transaction USING BRIN(created_at);
```

---

#### `ledger_entry` (new — central transaction ledger, append-only)

Every single naira movement that passes through FlowPilot writes exactly one row here. Specialized tables (`wallet_transaction`, `payout_candidate`, `platform_fee_transaction`) remain for domain-specific detail but all carry a `ledger_entry_id` FK back to this table.

This is the single table a CBN examiner, auditor, or reconciliation engineer queries first.

```sql
CREATE TABLE ledger_entry (
    -- ── Identity ──────────────────────────────────────────────────────────
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    internal_reference      VARCHAR(100) NOT NULL UNIQUE,
    -- FlowPilot's own globally-unique reference.
    -- Format: FP-{TYPE_PREFIX}-{YYYYMMDD}-{shortid}
    -- Examples:
    --   FP-PAY-20260418-a3f9b2   (payout disbursement)
    --   FP-WCR-20260418-c7d1e4   (wallet credit / top-up)
    --   FP-WDB-20260418-f2a8b1   (wallet debit for a run)
    --   FP-FEE-20260418-9e3c77   (platform fee)
    --   FP-REF-20260418-b4d2a9   (refund)

    client_reference        VARCHAR(100),
    -- Reference provided by the business at run creation (end-to-end ID, ISO 20022).
    -- Echoed back to payer on their statement.

    provider_reference      VARCHAR(100),
    -- Reference returned by the payment provider (Interswitch, Monnify).
    -- This is the reference the beneficiary's bank will see.

    session_reference       VARCHAR(100),
    -- Batch reference if this entry was part of a payout batch.

    -- ── Classification ────────────────────────────────────────────────────
    entry_type              VARCHAR(50) NOT NULL CHECK (entry_type IN (
                                'wallet_credit',        -- business tops up FlowPilot wallet via bank transfer
                                'wallet_reserve',       -- funds locked at run approval (not yet debited)
                                'wallet_settle',        -- reservation converted to debit after successful payouts
                                'wallet_release',       -- unused reservation returned to available balance
                                'payout_disbursement',  -- money sent to a beneficiary (mirrors wallet_settle)
                                'platform_fee',         -- FlowPilot 0.6% fee charged at settlement
                                'platform_fee_refund',  -- fee reversed for failed/released portion
                                'ai_credit_purchase',   -- business bought AI processing credits
                                'ai_credit_debit',      -- one credit consumed per run
                                'payee_receipt',        -- payee-side mirror of payout_disbursement
                                'reversal'              -- provider-initiated reversal after settlement
                            )),

    -- ── Amounts ───────────────────────────────────────────────────────────
    gross_amount            NUMERIC(18,2) NOT NULL CHECK (gross_amount > 0),
    -- The full face value of the transaction before fees.

    fee_amount              NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    -- Platform fee or provider charge applied to this entry.

    net_amount              NUMERIC(18,2) NOT NULL,
    -- gross_amount - fee_amount. Enforced at app layer on insert.
    -- For wallet_credit: the amount credited to wallet after any charges.
    -- For payout_disbursement: the amount the beneficiary actually receives.

    currency                CHAR(3) NOT NULL DEFAULT 'NGN',

    direction               VARCHAR(6) NOT NULL CHECK (direction IN ('credit', 'debit')),
    -- From FlowPilot / the business's perspective.
    -- 'credit' = money coming in or being released (wallet_credit, wallet_release, payee_receipt)
    -- 'debit'  = money going out or being locked  (wallet_reserve, wallet_settle, payout_disbursement, platform_fee)
    -- Note: wallet_reserve is direction='debit' (reduces available balance) but does not reduce actual balance
    -- until wallet_settle fires. The distinction is tracked in the wallet table via reserved_balance.

    -- ── Status ────────────────────────────────────────────────────────────
    status                  VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN (
                                'pending',      -- initiated, not yet confirmed
                                'processing',   -- submitted to provider, awaiting result
                                'completed',    -- confirmed success
                                'failed',       -- provider rejected or timed out
                                'reversed'      -- completed then reversed by provider or manually
                            )),

    failure_reason          TEXT,
    -- Populated when status = 'failed' or 'reversed'. Human-readable.

    -- ── Originator (Sender) ───────────────────────────────────────────────
    originator_type         VARCHAR(20) NOT NULL CHECK (originator_type IN (
                                'business',     -- a FlowPilot payer business
                                'external_bank',-- for incoming wallet top-ups
                                'system'        -- FlowPilot itself (for fees, credits)
                            )),

    originator_business_id  UUID REFERENCES business(id) ON DELETE RESTRICT,
    -- Populated when originator_type = 'business'.

    originator_name         VARCHAR(255),
    -- Full legal name of the sending entity.
    -- For businesses: the business_name.
    -- For external bank credits: the account holder name returned by the bank.

    originator_account_number VARCHAR(20),
    -- The sender's bank account number.
    -- For payout runs: the business's Interswitch source account.
    -- For wallet top-ups: the external bank account that transferred in.

    originator_bank_name    VARCHAR(128),
    originator_bank_code    VARCHAR(10),

    -- ── Beneficiary (Receiver) ────────────────────────────────────────────
    beneficiary_type        VARCHAR(20) NOT NULL CHECK (beneficiary_type IN (
                                'payee',        -- an individual or business being paid
                                'business',     -- FlowPilot wallet being credited
                                'system'        -- FlowPilot revenue (fees)
                            )),

    beneficiary_bank_account_id UUID REFERENCES payee_bank_account(id) ON DELETE SET NULL,
    -- FK to the verified bank account record. Populated for payout_disbursement entries.

    beneficiary_payee_profile_id UUID REFERENCES payee_profile(id) ON DELETE SET NULL,
    -- Populated if the beneficiary is a registered FlowPilot payee.

    beneficiary_name        VARCHAR(255),
    -- Name on the receiving bank account (returned by BAV / provider).

    beneficiary_account_number VARCHAR(20),
    -- Denormalized for audit speed — always readable even if payee_bank_account is deleted.

    beneficiary_bank_name   VARCHAR(128),
    beneficiary_bank_code   VARCHAR(10),

    -- ── Narration ─────────────────────────────────────────────────────────
    narration               TEXT,
    -- The narration that appears on the beneficiary's bank statement.
    -- For salary: "ACME LTD SALARY MAY 2026"
    -- For vendor: "ACME LTD - INVOICE INV-2026-0042"
    -- Truncated to 100 chars by provider if longer.

    internal_narration      TEXT,
    -- Internal note — not sent to provider or beneficiary.
    -- e.g. "Run #7 — Risk score 0.12 — Approved by Chidera"

    -- ── Context ───────────────────────────────────────────────────────────
    business_id             UUID REFERENCES business(id) ON DELETE RESTRICT,
    -- The payer business. Always populated except for system entries.

    run_id                  UUID REFERENCES agent_run(id) ON DELETE SET NULL,
    -- Populated for payout_disbursement and wallet_debit entries.

    purpose_code            VARCHAR(10),
    -- ISO 20022 purpose code.
    -- Common values: SALA (salary), SUPP (supplier), GOVT (government), CHAR (charity)

    -- ── Source Linkage ────────────────────────────────────────────────────
    -- Polymorphic back-reference to the specialized table that owns the detail.
    source_table            VARCHAR(64),
    -- e.g. 'wallet_transaction', 'payout_candidate', 'platform_fee_transaction'

    source_id               VARCHAR(64),
    -- UUID or BIGINT PK of the row in source_table (stored as text for flexibility).

    -- ── Timestamps ────────────────────────────────────────────────────────
    initiated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- When FlowPilot first created this entry.

    completed_at            TIMESTAMPTZ,
    -- When the provider confirmed the transaction as settled.

    value_date              DATE,
    -- The date the funds are considered available to the beneficiary.
    -- May differ from completed_at (e.g. next-day settlement).

    settlement_date         DATE,
    -- The interbank settlement date (NIP/NIBSS settlement window).

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
    -- Always equals initiated_at. Separate field for consistency with other tables.
);

-- Indexes
CREATE UNIQUE INDEX ledger_entry_internal_ref_idx   ON ledger_entry(internal_reference);
CREATE INDEX ledger_entry_client_ref_idx            ON ledger_entry(client_reference)
    WHERE client_reference IS NOT NULL;
CREATE INDEX ledger_entry_provider_ref_idx          ON ledger_entry(provider_reference)
    WHERE provider_reference IS NOT NULL;
CREATE INDEX ledger_entry_business_id_idx           ON ledger_entry(business_id);
CREATE INDEX ledger_entry_run_id_idx                ON ledger_entry(run_id);
CREATE INDEX ledger_entry_entry_type_idx            ON ledger_entry(entry_type);
CREATE INDEX ledger_entry_status_idx                ON ledger_entry(status);
CREATE INDEX ledger_entry_beneficiary_account_idx   ON ledger_entry(beneficiary_bank_account_id);
CREATE INDEX ledger_entry_payee_profile_idx         ON ledger_entry(beneficiary_payee_profile_id);
CREATE INDEX ledger_entry_initiated_at_idx          ON ledger_entry USING BRIN(initiated_at);
CREATE INDEX ledger_entry_value_date_idx            ON ledger_entry(value_date);
CREATE INDEX ledger_entry_originator_account_idx    ON ledger_entry(originator_account_number)
    WHERE originator_account_number IS NOT NULL;
```

**Immutability rule:** `ledger_entry` is **append-only**. Once a row is inserted:
- `status` may only be updated by the execution engine via a controlled service method — never directly
- All other columns are frozen at insert time
- No DELETE is permitted at the database or application layer

**How every other table links to it:**

```sql
-- Add to wallet_transaction:
ALTER TABLE wallet_transaction
    ADD COLUMN ledger_entry_id BIGINT REFERENCES ledger_entry(id) ON DELETE RESTRICT;

-- Add to payout_candidate (written at execution time, not creation):
ALTER TABLE payout_candidate
    ADD COLUMN ledger_entry_id BIGINT REFERENCES ledger_entry(id) ON DELETE RESTRICT;

-- Add to platform_fee_transaction:
ALTER TABLE platform_fee_transaction
    ADD COLUMN ledger_entry_id BIGINT REFERENCES ledger_entry(id) ON DELETE RESTRICT;

-- Add to ai_credit_transaction:
ALTER TABLE ai_credit_transaction
    ADD COLUMN ledger_entry_id BIGINT REFERENCES ledger_entry(id) ON DELETE RESTRICT;
```

**Sample queries this enables:**

```sql
-- Full financial statement for a business (CBN audit view)
SELECT
    internal_reference,
    client_reference,
    provider_reference,
    entry_type,
    originator_name,
    originator_account_number,
    originator_bank_name,
    beneficiary_name,
    beneficiary_account_number,
    beneficiary_bank_name,
    beneficiary_bank_code,
    gross_amount,
    fee_amount,
    net_amount,
    currency,
    direction,
    status,
    narration,
    purpose_code,
    value_date,
    settlement_date,
    initiated_at,
    completed_at
FROM ledger_entry
WHERE business_id = $1
ORDER BY initiated_at DESC;

-- All payments received by a specific bank account (payee view)
SELECT *
FROM ledger_entry
WHERE beneficiary_account_number = $1
  AND entry_type = 'payout_disbursement'
  AND status = 'completed'
ORDER BY initiated_at DESC;

-- All failed transactions in a date range (AML monitoring)
SELECT *
FROM ledger_entry
WHERE status = 'failed'
  AND initiated_at BETWEEN $1 AND $2
ORDER BY initiated_at DESC;

-- Total outflow for a business this month (KYC limit enforcement)
SELECT COALESCE(SUM(gross_amount), 0)
FROM ledger_entry
WHERE business_id = $1
  AND entry_type = 'payout_disbursement'
  AND status = 'completed'
  AND initiated_at >= date_trunc('month', now());
```

---

### 5.5 Compliance Domain

#### `suspicious_activity_report` (new — CBN SAR requirement)
```sql
CREATE TABLE suspicious_activity_report (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     UUID NOT NULL REFERENCES business(id) ON DELETE RESTRICT,
    run_id          UUID REFERENCES agent_run(id) ON DELETE SET NULL,
    candidate_id    UUID REFERENCES payout_candidate(id) ON DELETE SET NULL,
    report_type     VARCHAR(20) NOT NULL DEFAULT 'SAR'
                    CHECK (report_type IN ('SAR', 'STR', 'CTR')),
    -- SAR = Suspicious Activity Report
    -- STR = Suspicious Transaction Report
    -- CTR = Cash Transaction Report (for transactions above ₦5M)
    status          VARCHAR(20) NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'submitted', 'acknowledged')),
    description     TEXT NOT NULL,
    submitted_to    VARCHAR(100) DEFAULT 'NFIU',    -- Nigerian Financial Intelligence Unit
    submitted_at    TIMESTAMPTZ,
    nfiu_reference  VARCHAR(100),
    created_by      UUID REFERENCES "user"(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `consent_record` (new — NDPC Act 2023)
```sql
CREATE TABLE consent_record (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES "user"(id) ON DELETE SET NULL,
    payee_profile_id UUID REFERENCES payee_profile(id) ON DELETE SET NULL,
    purpose         VARCHAR(100) NOT NULL,
    -- 'account_creation' | 'kyc_processing' | 'payment_processing'
    -- 'marketing' | 'data_sharing' | 'payee_portal'
    policy_version  VARCHAR(20) NOT NULL,
    is_granted      BOOLEAN NOT NULL,
    ip_address      INET,
    user_agent      TEXT,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ,
    CHECK (user_id IS NOT NULL OR payee_profile_id IS NOT NULL)
);
CREATE INDEX consent_record_user_id_idx ON consent_record(user_id);
CREATE INDEX consent_record_payee_profile_id_idx ON consent_record(payee_profile_id);
```

---

## 6. Payee Self-Service Portal — Full Design

### 6.1 Overview

The Payee Portal introduces a new account type (`account_type = 'payee'` on the `user` table) with a completely separate experience from payer accounts. The same `user` table is used for auth, but a separate `payee_profile` table holds all payee-specific data.

**Entry point:** Payment notification email contains a CTA button. Payee clicks it, enters email, verifies, and their payment history (matched by bank account) is immediately visible.

---

### 6.2 New Tables

#### `payee_profile` (new)
```sql
CREATE TABLE payee_profile (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL UNIQUE REFERENCES "user"(id) ON DELETE CASCADE,
    display_name        VARCHAR(100) NOT NULL,
    business_name       VARCHAR(255),              -- for freelancers/contractors
    -- e.g. "Chidera Ozigbo" or "Chidera Consulting Ltd"
    tier                SMALLINT NOT NULL DEFAULT 1
                        CHECK (tier IN (1, 2, 3)),
    -- Tier 1: email + bank account only
    -- Tier 2: + display name / business name (unlocks invoices)
    -- Tier 3: + NIN/BVN (unlocks income statements)
    kyc_status          VARCHAR(20) NOT NULL DEFAULT 'not_verified'
                        CHECK (kyc_status IN ('not_verified', 'pending', 'verified')),
    -- Tier 3 KYC status
    id_type             VARCHAR(10) CHECK (id_type IN ('nin', 'bvn')),
    id_value_hash       VARCHAR(255),              -- bcrypt hash of NIN/BVN — never store raw
    id_verified_at      TIMESTAMPTZ,
    invoice_prefix      VARCHAR(10),               -- e.g. "INV" — for invoice number generation
    total_received      NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    -- Running total of all verified payments received via FlowPilot
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

#### `payee_payer_relationship` (new — tracks which businesses pay a given payee)
```sql
CREATE TABLE payee_payer_relationship (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payee_profile_id    UUID NOT NULL REFERENCES payee_profile(id) ON DELETE CASCADE,
    business_id         UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    -- Linked if the payer has this payee in their saved_recipient list
    saved_recipient_id  UUID REFERENCES saved_recipient(id) ON DELETE SET NULL,
    first_payment_at    TIMESTAMPTZ,
    last_payment_at     TIMESTAMPTZ,
    total_received      NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    payment_count       INTEGER NOT NULL DEFAULT 0,
    share_schedule      BOOLEAN NOT NULL DEFAULT false,
    -- If true, payer consents to share upcoming payment schedules with this payee
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (payee_profile_id, business_id)
);
CREATE INDEX payee_payer_rel_payee_idx ON payee_payer_relationship(payee_profile_id);
CREATE INDEX payee_payer_rel_business_idx ON payee_payer_relationship(business_id);
```

---

#### `invoice` (new)
```sql
CREATE TABLE invoice (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payee_profile_id    UUID NOT NULL REFERENCES payee_profile(id) ON DELETE CASCADE,
    invoice_number      VARCHAR(50) NOT NULL UNIQUE,
    -- Generated: {invoice_prefix}-{YYYY}-{NNNN} e.g. "INV-2026-0042"
    -- If sent to a FlowPilot payer:
    payer_business_id   UUID REFERENCES business(id) ON DELETE SET NULL,
    -- If sent to an external (non-FlowPilot) payer:
    external_payer_name VARCHAR(255),
    external_payer_email VARCHAR(255),
    currency            CHAR(3) NOT NULL DEFAULT 'NGN',
    subtotal            NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    tax_amount          NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    discount_amount     NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    total_amount        NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    amount_paid         NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    status              VARCHAR(20) NOT NULL DEFAULT 'draft'
                        CHECK (status IN (
                            'draft', 'sent', 'viewed', 'partially_paid',
                            'paid', 'overdue', 'cancelled', 'voided'
                        )),
    issue_date          DATE NOT NULL DEFAULT CURRENT_DATE,
    due_date            DATE NOT NULL,
    paid_at             TIMESTAMPTZ,
    -- If paid by a FlowPilot payout run:
    payout_candidate_id UUID REFERENCES payout_candidate(id) ON DELETE SET NULL,
    notes               TEXT,
    payment_terms       TEXT,
    public_token        VARCHAR(64) UNIQUE,
    -- Short token for the hosted invoice URL (unauthenticated access)
    is_recurring        BOOLEAN NOT NULL DEFAULT false,
    recurrence_rule     JSONB,
    -- Stores cron expression, frequency, next generation date
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX invoice_payee_profile_id_idx ON invoice(payee_profile_id);
CREATE INDEX invoice_payer_business_id_idx ON invoice(payer_business_id);
CREATE INDEX invoice_status_idx ON invoice(status);
CREATE INDEX invoice_due_date_idx ON invoice(due_date);
CREATE INDEX invoice_public_token_idx ON invoice(public_token);
```

---

#### `invoice_line_item` (new)
```sql
CREATE TABLE invoice_line_item (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id      UUID NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
    description     VARCHAR(500) NOT NULL,
    quantity        NUMERIC(10,2) NOT NULL DEFAULT 1,
    unit_price      NUMERIC(18,2) NOT NULL,
    line_total      NUMERIC(18,2) NOT NULL,
    -- Always = quantity * unit_price, enforced at app layer
    sort_order      SMALLINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX invoice_line_item_invoice_id_idx ON invoice_line_item(invoice_id);
```

---

#### `invoice_activity` (new — audit trail for each invoice)
```sql
CREATE TABLE invoice_activity (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id      UUID NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
    event_type      VARCHAR(50) NOT NULL,
    -- 'created' | 'sent' | 'viewed' | 'payment_received'
    -- 'reminder_sent' | 'cancelled' | 'overdue_flagged'
    actor_id        UUID REFERENCES "user"(id) ON DELETE SET NULL,
    -- Null for system-generated events
    metadata        JSONB,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX invoice_activity_invoice_id_idx ON invoice_activity(invoice_id);
```

---

#### `payment_request` (new — lighter than an invoice)
```sql
CREATE TABLE payment_request (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payee_profile_id    UUID NOT NULL REFERENCES payee_profile(id) ON DELETE CASCADE,
    payer_business_id   UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    bank_account_id     UUID NOT NULL REFERENCES payee_bank_account(id),
    amount              NUMERIC(18,2) NOT NULL CHECK (amount > 0),
    currency            CHAR(3) NOT NULL DEFAULT 'NGN',
    description         TEXT NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'approved', 'paid', 'rejected', 'cancelled', 'expired'
                        )),
    expires_at          TIMESTAMPTZ,
    approved_by         UUID REFERENCES "user"(id) ON DELETE SET NULL,
    approved_at         TIMESTAMPTZ,
    payout_candidate_id UUID REFERENCES payout_candidate(id) ON DELETE SET NULL,
    -- Populated when the payer approves and creates a payout candidate
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX payment_request_payee_profile_id_idx ON payment_request(payee_profile_id);
CREATE INDEX payment_request_payer_business_id_idx ON payment_request(payer_business_id);
CREATE INDEX payment_request_status_idx ON payment_request(status);
```

---

#### `income_statement` (new — downloadable proof of income)
```sql
CREATE TABLE income_statement (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payee_profile_id    UUID NOT NULL REFERENCES payee_profile(id) ON DELETE CASCADE,
    period_type         VARCHAR(10) NOT NULL CHECK (period_type IN ('monthly', 'annual')),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    total_received      NUMERIC(18,2) NOT NULL,
    payer_count         INTEGER NOT NULL DEFAULT 0,
    payment_count       INTEGER NOT NULL DEFAULT 0,
    currency            CHAR(3) NOT NULL DEFAULT 'NGN',
    document_key        VARCHAR(512),              -- MinIO key of generated PDF
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (payee_profile_id, period_type, period_start)
);
CREATE INDEX income_statement_payee_profile_id_idx ON income_statement(payee_profile_id);
```

---

#### `payee_payment_receipt` (new — per-payment receipt for payees)
```sql
CREATE TABLE payee_payment_receipt (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payee_profile_id    UUID NOT NULL REFERENCES payee_profile(id) ON DELETE CASCADE,
    payout_candidate_id UUID NOT NULL REFERENCES payout_candidate(id) ON DELETE RESTRICT,
    bank_account_id     UUID NOT NULL REFERENCES payee_bank_account(id),
    payer_business_id   UUID NOT NULL REFERENCES business(id) ON DELETE RESTRICT,
    amount              NUMERIC(18,2) NOT NULL,
    currency            CHAR(3) NOT NULL DEFAULT 'NGN',
    purpose             VARCHAR(255),
    provider_reference  VARCHAR(100),
    receipt_number      VARCHAR(50) UNIQUE,
    -- Generated: FP-{YYYYMMDD}-{shortid}
    document_key        VARCHAR(512),              -- PDF in MinIO
    paid_at             TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX payee_payment_receipt_payee_profile_idx ON payee_payment_receipt(payee_profile_id);
CREATE INDEX payee_payment_receipt_paid_at_idx ON payee_payment_receipt USING BRIN(paid_at);
```

---

### 6.3 Payee Portal API Routes

New router: `app/api/routes/payee.py` — prefix `/payee`

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/payee/register` | None | Create payee account from email + bank account |
| `GET` | `/payee/register/verify-email` | Token | Verify email from link |
| `GET` | `/payee/profile` | Payee JWT | Get payee profile + tier |
| `PATCH` | `/payee/profile` | Payee JWT | Update display name / business name |
| `GET` | `/payee/payments` | Payee JWT | Paginated list of received payments |
| `GET` | `/payee/payments/{id}/receipt` | Payee JWT | Download payment receipt PDF |
| `GET` | `/payee/payers` | Payee JWT | List of businesses that have paid them |
| `GET` | `/payee/bank-accounts` | Payee JWT | List verified bank accounts |
| `POST` | `/payee/bank-accounts` | Payee JWT | Add + verify a new bank account (runs BAV) |
| `PATCH` | `/payee/bank-accounts/{id}/set-primary` | Payee JWT | Set primary account |
| `POST` | `/payee/kyc/identity` | Payee JWT | Submit NIN/BVN for Tier 3 |
| `GET` | `/payee/invoices` | Payee JWT | List invoices |
| `POST` | `/payee/invoices` | Payee JWT | Create invoice |
| `GET` | `/payee/invoices/{id}` | Payee JWT | Get invoice detail |
| `PATCH` | `/payee/invoices/{id}` | Payee JWT | Update invoice (draft only) |
| `POST` | `/payee/invoices/{id}/send` | Payee JWT | Mark as sent, notify payer |
| `POST` | `/payee/invoices/{id}/cancel` | Payee JWT | Cancel invoice |
| `GET` | `/payee/invoices/public/{token}` | None | Public hosted invoice page |
| `POST` | `/payee/payment-requests` | Payee JWT | Create payment request to a payer |
| `GET` | `/payee/payment-requests` | Payee JWT | List payment requests |
| `DELETE` | `/payee/payment-requests/{id}` | Payee JWT | Cancel a pending request |
| `GET` | `/payee/income/summary` | Payee JWT | Monthly/annual income breakdown |
| `POST` | `/payee/income/statement` | Payee JWT (Tier 3) | Generate income statement PDF |

New router: `app/api/routes/payer_invoices.py` — prefix `/invoices` (payer-side)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/invoices/received` | Payer JWT | List invoices received from payees |
| `POST` | `/invoices/{id}/approve` | Payer JWT (owner/approver) | Approve invoice → creates payout candidate |
| `POST` | `/invoices/{id}/reject` | Payer JWT (owner/approver) | Reject invoice with reason |

New router: `app/api/routes/payer_payment_requests.py` — prefix `/payment-requests`

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/payment-requests` | Payer JWT | List incoming payment requests |
| `POST` | `/payment-requests/{id}/approve` | Payer JWT | Approve → creates payout candidate |
| `POST` | `/payment-requests/{id}/reject` | Payer JWT | Reject with reason |

---

### 6.4 Payee Onboarding Flow

```
Step 1 — Trigger
  Payment notification email contains:
  "Received ₦150,000 from Acme Ltd. Track all your payments → [Create Free Account]"
  Link: /payee/join?ref={payout_candidate_id}

Step 2 — Registration (no password required initially)
  POST /payee/register
  Body: { email, account_number, institution_code }
  - Runs BAV on account_number + institution_code
  - If BAV passes: creates user (account_type='payee') + payee_profile (tier=1)
  - Creates payee_bank_account record
  - Sends email verification link
  - Backfills payment history (matches by account_number + institution_code)
  - Records consent: { purpose: 'payee_portal', version: '1.0' }

Step 3 — Email Verification
  GET /payee/register/verify-email?token={token}
  - Marks user.email_verified_at = now()
  - Returns JWT — user lands on payee dashboard

Step 4 — (Optional) Tier 2: Add display name / business name
  PATCH /payee/profile
  Body: { display_name, business_name }
  - Upgrades tier to 2
  - Unlocks: invoice creation, payment requests

Step 5 — (Optional) Tier 3: Submit NIN/BVN
  POST /payee/kyc/identity
  Body: { id_type: 'bvn' | 'nin', id_value }
  - Verifies via Monnify (same as payer individual KYC Level 1)
  - On success: upgrades tier to 3
  - Unlocks: income statements, higher invoice limits
```

---

### 6.5 How Payout Candidate Links to Payee

When a payout run executes and a payment is successful:

```python
# In execution agent — post-execution hook
async def _link_payee_after_payment(candidate: PayoutCandidateModel, session):
    # Find payee_bank_account by account_number + institution_code
    bank_account = await session.execute(
        select(PayeeBankAccountModel).where(
            PayeeBankAccountModel.account_number == candidate.bank_account.account_number,
            PayeeBankAccountModel.institution_code == candidate.bank_account.institution_code
        )
    )
    if bank_account and bank_account.payee_profile_id:
        # Create receipt
        receipt = PayeePaymentReceiptModel(
            payee_profile_id=bank_account.payee_profile_id,
            payout_candidate_id=candidate.id,
            bank_account_id=bank_account.id,
            payer_business_id=candidate.business_id,
            amount=candidate.amount,
            purpose=candidate.purpose,
            paid_at=now(),
        )
        session.add(receipt)
        # Update payee_payer_relationship
        # Send payee payment notification
```

---

### 6.6 Network Effect Logic

When a payer creates a new run and enters a recipient:

```python
# In run creation — before BAV
bank_account = await session.execute(
    select(PayeeBankAccountModel).where(
        PayeeBankAccountModel.account_number == input_account_number,
        PayeeBankAccountModel.institution_code == input_institution_code
    )
)
if bank_account and bank_account.is_bav_verified:
    # Skip BAV — already verified
    # Pre-populate candidate with verified name
    # Show payer: "✓ Verified FlowPilot payee — Chidera Ozigbo"
```

Once 1,000+ payees are on the platform, payers skip BAV for a significant portion of their payroll. This is the compounding moat.

---

## 7. Implementation Phases

### Phase 1 — Foundation Normalization (Do First)
**Goal:** Fix critical structural problems without breaking existing functionality.

1. Create `user_profile` table — migrate columns from `user`
2. Create `user_mfa` table — migrate 2FA columns from `user`
3. Create `user_oauth_provider` table — migrate OAuth columns from `user`
4. Create `user_notification_preference` table — migrate JSONB from `user`
5. Create `business_profile` table — migrate columns from `business`
6. Create `business_address` table — migrate location columns from `business`
7. Create `business_virtual_account` table — migrate virtual account columns from `business`
8. Create `business_payment_policy` table — migrate from `business_config`
9. Create `business_security_policy` table — migrate from `business_config`
10. Create `business_use_case` table — replace JSONB array
11. Write Alembic migrations for each (keep old columns with `NOT NULL` relaxed during transition)
12. Update all repositories and routes to use new tables
13. Drop old columns once all services are migrated

**Estimated effort:** 2–3 weeks

---

### Phase 2 — KYC Normalization
**Goal:** Fix the worst structural violations in KYC.

1. Create `kyc_document` table — replace all `_key` columns on `kyc_submission`
2. Create `kyc_principal` table — replace director/trustee/partner/officer columns
3. Migrate existing `KycSubmissionModel` data into new tables
4. Create `kyc_verification_level` table — replace `individual_kyc_submission` level columns
5. Migrate existing individual KYC data
6. Create `kyc_tier_limit` table — seed from `kyc_limits.py`
7. Update `/kyc/*` routes to use new schema
8. Remove old tables

**Estimated effort:** 1–2 weeks

---

### Phase 3 — Beneficiary Normalization & Central Ledger
**Goal:** Create `payee_bank_account` as the single source of truth for beneficiary identity and `ledger_entry` as the single source of truth for all money movements.

1. Create `payee_bank_account` table
2. Backfill from existing `payout_candidate`, `beneficiary_reputation`, `saved_recipient`
3. Add `bank_account_id` FK to `payout_candidate` — keep raw columns temporarily
4. Update `beneficiary_reputation` — replace `account_number`/`bank_code`/`beneficiary_name` with `bank_account_id`
5. Update `run_outcome_memory` similarly
6. Update `payout_compliance_record` similarly
7. Create `platform_fee_transaction` table — backfill from `agent_run`
8. **Create `ledger_entry` table** — the central transaction ledger
9. Add `ledger_entry_id` FK to `wallet_transaction`, `payout_candidate`, `platform_fee_transaction`, `ai_credit_transaction`
10. Backfill `ledger_entry` rows from all existing transaction records (write backfill script `scripts/migrations/backfill_ledger_entries.py`)
11. Update execution agent to write `ledger_entry` before writing to specialized table (write-ahead pattern)
12. Update wallet service to write `ledger_entry` on every credit/debit
13. Remove redundant raw beneficiary columns from `payout_candidate` once migration is verified

**Estimated effort:** 1–2 weeks

---

### Phase 4 — Compliance Infrastructure
**Goal:** Meet CBN audit and NDPC requirements before next regulatory review.

1. Create `user_audit_event` table
2. Wire event logging into all auth routes (login, logout, password change)
3. Wire into team management routes (invite, remove, role change)
4. Wire into config routes (payment policy change, 2FA enforcement)
5. Wire into KYC routes (submit, verify, reject)
6. Create `consent_record` table — record consent at registration and payee onboarding
7. Create `suspicious_activity_report` table
8. Create `kyc_tier_limit` table with CBN limits seeded
9. Create `data_subject_request` table for NDPC data access/erasure requests

**Estimated effort:** 1 week

---

### Phase 5 — Payee Self-Service Portal
**Goal:** Build the new payee experience end-to-end.

1. Add `account_type` column to `user` — distinguish `payer` from `payee`
2. Create `payee_profile` table
3. Create `payee_payer_relationship` table
4. Create `payee_bank_account` relationship to `payee_profile`
5. Build `/payee/register` and email verification flow
6. Build payee JWT auth (separate from payer JWTs — include `account_type` claim)
7. Build `/payee/payments` — backfilled history on signup
8. Build bank account management with BAV
9. Create `invoice` + `invoice_line_item` + `invoice_activity` tables
10. Build invoice CRUD + `/payee/invoices/{id}/send`
11. Build `/invoices/received` for payers
12. Build approve invoice → create payout candidate flow
13. Create `payment_request` table
14. Build payment request flow (payee creates, payer approves → payout candidate)
15. Create `income_statement` + `payee_payment_receipt` tables
16. Build income summary endpoint
17. Build income statement PDF generation (Tier 3 only)
18. Wire network effect: check `payee_bank_account` before BAV in run creation
19. Send notification to payee when payment lands
20. Build public invoice page (unauthenticated `/payee/invoices/public/{token}`)

**Estimated effort:** 4–5 weeks

---

## 8. Migration Strategy

### Guiding principles

1. **No big-bang migrations.** Each phase runs independently. Old and new columns coexist until migration is confirmed stable.
2. **Zero downtime.** All Alembic migrations add nullable columns first. Application code writes to both old and new columns. Once verified, old columns are dropped.
3. **Immutable tables stay immutable.** No migration will run UPDATE or DELETE on `ledger_entry`, `audit_log`, `wallet_transaction`, `payout_execution`, `api_call_log`, `user_audit_event`, or `platform_fee_transaction`. The `ledger_entry` table is the most critical — treat it as a permanent, tamper-evident record. If a correction is needed, a new reversal row is inserted, never an update to an existing row.
4. **Data is never deleted.** For KYC tables, old data is migrated to new tables and the old tables are renamed with `_deprecated` suffix before eventual drop.
5. **Every migration is reversible.** Each Alembic migration has a `downgrade()` that restores the previous state.

### Alembic naming convention

```
{YYYY}_{MM}_{DD}_{HHMM}-{phase}_{description}.py

Examples:
2026_04_20_1000-p1_create_user_profile.py
2026_04_20_1100-p1_migrate_user_profile_data.py
2026_04_20_1200-p1_drop_user_profile_columns_from_user.py
```

### Backfill scripts

For each data migration step, a standalone Python script in `scripts/migrations/` handles the data move with progress logging, idempotency (safe to re-run), and batch size limits to avoid lock contention on large tables.

```
scripts/migrations/
  backfill_user_profile.py
  backfill_user_mfa.py
  backfill_business_profile.py
  backfill_kyc_documents.py
  backfill_kyc_principals.py
  backfill_payee_bank_accounts.py
  backfill_platform_fees.py
```

---

## 9. Financial Security & Integrity Gaps

> This section documents every security concern in the current system — what is already handled, what is partially handled, and what is completely missing. Every item marked **MISSING** or **PARTIAL** must be resolved before this platform handles production-scale customer funds.

---

### 9.1 Legend

| Status | Meaning |
|---|---|
| ✅ DONE | Implemented correctly in current codebase |
| ⚠️ PARTIAL | Exists but has known gaps or edge cases |
| ❌ MISSING | Not implemented — must be built |

---

### 9.2 Race Conditions

#### Wallet debit race condition
**Status: ✅ DONE**

`wallet_repository.py` uses `SELECT ... FOR UPDATE` on the wallet row before every debit. This is a row-level pessimistic lock that blocks any concurrent session from reading the same pre-debit balance. Two simultaneous payout runs cannot both pass the balance check and both debit — the second will block until the first commits, then read the updated (lower) balance.

```python
# wallet_repository.py — _get_locked()
select(WalletModel)
    .where(WalletModel.business_id == business_id)
    .with_for_update()   # ← row-level lock
```

---

#### Wallet credit race condition
**Status: ✅ DONE**

Credits do not need a lock because adding to a balance is commutative. The `UNIQUE` constraint on `wallet_transaction.reference` prevents a duplicate credit from processing — whichever request wins the DB constraint, the other gets `IntegrityError` and is treated as a replay.

---

#### KYC monthly limit race condition
**Status: ❌ MISSING — HIGH PRIORITY**

The monthly payout limit check reads `kyc_limit_tracker.monthly_payout_used` and compares it against the KYC tier cap. But this check is done **without a lock**. If two runs execute simultaneously:

1. Run A reads `monthly_payout_used = ₦900,000`. Limit is ₦1,000,000. ₦200,000 run passes.
2. Run B reads `monthly_payout_used = ₦900,000` before Run A commits. ₦200,000 run also passes.
3. Both execute. Final `monthly_payout_used = ₦1,100,000`. **Limit breached.**

**Fix required:**
```sql
-- Add to kyc_limit_tracker before the check:
SELECT * FROM kyc_limit_tracker
WHERE business_id = $1
FOR UPDATE;  -- ← row-level lock same as wallet

-- Then check and update atomically within the same transaction
```

This is a CBN compliance violation. Monthly limits exist specifically to control exposure under each KYC tier.

---

#### Payout approval double-fire race condition
**Status: ❌ MISSING**

Two approvers can click "Approve" simultaneously on the same run. Without a lock, both requests could read `approval_status = 'awaiting_approval'`, both pass the check, and both trigger execution — resulting in the same payouts being submitted to Interswitch twice.

**Fix required:**

Use an atomic status transition with a DB-level guard:
```sql
UPDATE agent_run
SET status = 'executing', approved_by = $user_id, approved_at = now()
WHERE id = $run_id
  AND status = 'awaiting_approval'  -- ← only one can win this
RETURNING id;
-- If 0 rows returned, another approver got there first → return 409
```

---

#### Concurrent run creation
**Status: ⚠️ PARTIAL**

There is no guard preventing a business from creating two runs at the exact same time. If both runs debit the wallet, the `SELECT FOR UPDATE` prevents double-spend, but both runs may still be created referencing the same time window of transactions, causing confusion in reconciliation.

**Fix:** Add a `UNIQUE` constraint or advisory lock on `(business_id, status)` where `status IN ('pending', 'executing', 'awaiting_approval')` — prevent more than one active run per business at a time (configurable policy).

---

### 9.3 Idempotency

#### Wallet credit idempotency
**Status: ✅ DONE**

`wallet_transaction.reference` has a `UNIQUE` constraint. `repo.credit()` checks for an existing transaction with that reference before creating one. If a webhook fires twice for the same payment, the second call returns `(existing_tx, created=False)` — no duplicate credit.

---

#### Monnify webhook replay protection
**Status: ✅ DONE**

`monnify_webhooks.py` uses Redis with `SET ... NX EX 86400` (set-if-not-exists, 24-hour TTL) keyed by `payment_reference + body_digest`. First delivery sets the key and processes. Any replay within 24 hours is silently dropped.

---

#### Payout execution idempotency
**Status: ⚠️ PARTIAL**

`payout_candidate.client_reference` has a `UNIQUE` constraint and is used as the `transactionReference` sent to Interswitch. If the execution agent submits a payout and then crashes before recording the response, on restart it will see `execution_status = 'pending'` and requery Interswitch — it will **not** resubmit. This is correct.

However, there is no background job that automatically picks up candidates stuck in `execution_status = 'pending'` after a crash. If the server crashes mid-execution, those candidates stay pending indefinitely.

**Fix required:** A scheduled job (every 5 minutes) that:
1. Finds candidates with `execution_status = 'pending'` and `updated_at < now() - interval '10 minutes'`
2. Calls `requery_payout()` for each
3. Updates status to `success` or `failed` based on provider response

---

#### Webhook missed delivery (silent drop on DB error)
**Status: ❌ MISSING**

In `monnify_webhooks.py`, if the database commit fails after the Redis guard passes, the function returns `{"ok": True}` to Monnify. Monnify marks the webhook as delivered. The payment is never credited to the wallet. **Customer money is lost.**

**Fix required:** Outbox pattern for webhook processing:
1. Write raw webhook payload to a `webhook_inbound` table inside a DB transaction
2. Return `200 OK` to Monnify
3. A background worker reads `webhook_inbound` and processes (credits wallet)
4. Only delete from `webhook_inbound` after successful processing

This guarantees at-least-once processing with no silent data loss.

---

### 9.4 Transaction Reversal

#### Failed payout wallet reversal
**Status: ❌ MISSING — CRITICAL**

This is the most serious gap. When a run executes:
1. The wallet is debited for the total payout amount + platform fee upfront
2. Payouts are submitted to Interswitch one by one
3. Some payouts may fail (wrong account, bank offline, etc.)

**Currently, failed payout amounts are never credited back to the wallet.** The business pays for payouts that never landed.

**Fix required:**

After each payout candidate is marked `failed`, immediately issue a wallet credit reversal:
```python
async def _refund_failed_candidate(candidate, session):
    repo = WalletRepository(session)
    reversal_ref = f"reversal_{candidate.client_reference}"
    await repo.credit(
        business_id=candidate.business_id,
        amount=candidate.amount,
        reference=reversal_ref,
        description=f"Reversal — failed payout to {candidate.beneficiary_name}",
        run_id=candidate.run_id,
    )
    # Also write a ledger_entry with entry_type='reversal'
    # Also refund the platform fee proportionally
```

---

#### Platform fee reversal on failed payout
**Status: ❌ MISSING**

Platform fees are charged on execution. If a payout fails, the fee for that specific candidate should be refunded. Currently there is no mechanism for this.

**Fix required:** When marking a candidate as `failed`, calculate the fee that was charged for it (pro-rata of total batch fee) and issue a credit back to the wallet with `entry_type = 'platform_fee_refund'` in `ledger_entry`.

---

#### Provider-initiated reversal (NIP reversal)
**Status: ❌ MISSING**

Banks can reverse transactions hours or days after settlement. Interswitch may notify FlowPilot via webhook that a previously `SUCCESSFUL` payout was reversed. There is no webhook handler for this event and no reversal flow in the database.

**Fix required:**
1. Handle `REVERSAL` event type in the Interswitch/Monnify webhook
2. When received:
   - Create a `wallet_transaction` credit for the reversed amount
   - Create a `ledger_entry` with `entry_type = 'reversal'`
   - Update `payout_candidate.execution_status = 'reversed'`
   - Notify the business owner
   - Flag the candidate for AML review (reversals are a red flag)

---

#### Stuck run recovery (execution timeout)
**Status: ❌ MISSING**

If a run is stuck in `status = 'executing'` for more than 30 minutes (server crash, network partition), it will never resolve. The wallet has already been debited. The business cannot create a new run.

**Fix required:** A watchdog job that:
1. Finds runs with `status = 'executing'` and `updated_at < now() - interval '30 minutes'`
2. Requeries all pending candidates in the run
3. If provider confirms success/failure: marks candidates and updates run status
4. If provider is unreachable: marks run `status = 'requires_followup'` and notifies the business

---

### 9.5 Data Type Safety

#### Float used for money amounts
**Status: ❌ MISSING — HIGH PRIORITY**

In `internal_payment.py`:
```python
class SingleTransferRequest(BaseModel):
    amount: float    # ← WRONG
```

Floats cannot represent all decimal values precisely. `0.1 + 0.2 = 0.30000000000000004` in floating point. For financial amounts, this causes rounding errors that compound over thousands of transactions.

**Fix required:** All money fields must use `Decimal`:
```python
from decimal import Decimal
amount: Decimal
```

This applies to **every** Pydantic model, every function parameter, and every internal calculation that involves money. Never use `float` for money.

---

#### Balance integrity constraint missing
**Status: ❌ MISSING**

The database has no constraint verifying that `balance_after = balance_before + amount` (for credits) or `balance_after = balance_before - amount` (for debits). This invariant is enforced only at the application layer.

**Fix required:**
```sql
ALTER TABLE wallet_transaction
ADD CONSTRAINT wallet_tx_balance_integrity CHECK (
    (type = 'credit' AND balance_after = balance_before + amount) OR
    (type = 'debit'  AND balance_after = balance_before - amount)
);
```

---

### 9.6 Authentication Security

#### TOTP replay attack
**Status: ❌ MISSING**

A TOTP code is valid for 30 seconds. If an attacker intercepts a valid TOTP code and uses it within that window, they can authenticate. The current implementation does not track used TOTP codes.

**Fix required:** Store used TOTP codes in Redis with a 60-second TTL (30s current window + 30s drift allowance):
```python
key = f"totp:used:{user_id}:{totp_code}"
used = await redis.set(key, "1", ex=60, nx=True)
if not used:
    raise HTTPException(401, "TOTP code already used")
```

---

#### Session invalidation on password/PIN change
**Status: ❌ MISSING**

When a user changes their password, enables 2FA, or resets their approval PIN, all existing JWT sessions should be invalidated. Currently, old tokens remain valid until their natural expiry.

**Fix required:** Add a `security_version` integer to `user_mfa`. Increment it on any security change. Include `security_version` as a claim in the JWT. Validate the claim against the DB on every authenticated request. Mismatch = force re-login.

---

#### Rate limiting on auth endpoints
**Status: ❌ MISSING**

There is no rate limiting on:
- `POST /auth/login` — brute force password guessing
- `POST /auth/verify-totp` — brute force TOTP codes
- `POST /auth/forgot-password` — email enumeration + flooding
- `POST /kyc/individual/level1` — BVN/NIN enumeration

**Fix required:** Use a Redis-backed rate limiter (e.g. `slowapi`) with:
- Login: 10 attempts per IP per 15 minutes
- TOTP: 5 attempts per user per 15 minutes (lock account on breach)
- Password reset: 3 requests per email per hour
- KYC submission: 5 attempts per business per day

---

#### Webhook signature timing attack
**Status: ⚠️ PARTIAL**

The Monnify webhook verifies signatures, but if the comparison uses a standard `==` string comparison (rather than `hmac.compare_digest`), it is vulnerable to timing attacks where an attacker can infer the correct signature byte by byte based on response time differences.

**Fix required:**
```python
import hmac
# Replace any == comparison with:
if not hmac.compare_digest(computed_signature, received_signature):
    raise HTTPException(401, "Invalid signature")
```

---

#### API key comparison
**Status: ⚠️ PARTIAL — needs verification**

API key validation hashes the incoming key and compares hashes. Hash comparison is safe by nature (fixed length), but any intermediate string comparisons should use `hmac.compare_digest`.

---

### 9.7 Missing Operational Safeguards

#### No dead letter queue for failed background tasks
**Status: ❌ MISSING**

`asyncio.create_task()` is used throughout for email sending and notification delivery. If a task fails (exception raised), it is silently dropped — no retry, no alerting, no audit record.

**Fix required:** All background tasks should use the `notification_outbox` pattern that already exists in the codebase — write to the outbox table, let a worker process it with retries and failure tracking. Extend this pattern to cover all background work.

---

#### No payout amount reconciliation at run close
**Status: ❌ MISSING**

When a run completes, there is no process that verifies:
- Total wallet debited = sum of successful payouts + platform fees + sum of reversals
- Every candidate has a final terminal status (`success`, `failed`, `reversed`)

**Fix required:** An `audit_agent` reconciliation step at run close that:
1. Sums all `ledger_entry` rows for the run
2. Verifies debits = credits + net payouts
3. Flags any discrepancy as a `suspicious_activity_report`
4. Only marks the run `completed` if reconciliation passes

---

#### No dual-control on large payouts
**Status: ❌ MISSING**

For payout runs above a certain threshold (e.g. ₦10M total), best practice and CBN guidelines recommend a second independent approval (dual control / four-eyes principle). The current approval system only requires one approver.

**Fix required:** Add to `business_payment_policy`:
```sql
dual_control_threshold  NUMERIC(18,2),  -- e.g. 10,000,000.00
-- Runs above this amount require 2 independent approvers
```
The approval flow checks if `total_run_amount > dual_control_threshold` and, if so, requires a second `approval_override` from a different user.

---

### 9.7b Disposable / Temporary Email Rejection

**Status: ❌ MISSING**

Users registering or logging in with disposable/temporary email addresses (Mailinator, Guerrilla Mail, 10 Minute Mail, Temp Mail, YOPmail, Throwam, etc.) must be blocked at the point of entry. These services generate inboxes that expire within minutes, making it impossible to:

- Send KYC verification emails that will actually be received
- Reach the user if a suspicious transaction is flagged
- Recover an account via password reset
- Fulfil NDPC data subject communication requirements
- Contact the account holder for AML purposes (CBN requirement)

A user who registers with a disposable email effectively registers anonymously. This is unacceptable for a regulated payment platform.

---

**Where to enforce:**

Block at every point where an email address is first accepted:

| Endpoint | Action |
|---|---|
| `POST /auth/register` | Reject registration — return `422 Unprocessable Entity` |
| `POST /auth/register-via-invite` | Reject — invitation was sent to a temp address |
| `POST /team/invite` | Prevent owner from inviting a temp email address |
| `POST /payee/register` | Reject payee onboarding |
| `POST /auth/forgot-password` | Silently skip (do not confirm whether the address exists) |

**Do not** enforce on `POST /auth/login` — the user is already registered at that point. Blocking login retroactively would lock out users who somehow slipped through. Enforce only at registration.

---

**Implementation — two-layer approach:**

**Layer 1 — Local blocklist (fast, zero latency, no external dependency)**

Maintain a curated list of known disposable email domains in a config file (`src/config/blocked_email_domains.py`). This covers the most common offenders and works even if the external API is down.

```python
# src/config/blocked_email_domains.py

BLOCKED_DOMAINS: set[str] = {
    # ── High-volume disposable providers ─────────────────────────────────
    "mailinator.com", "guerrillamail.com", "guerrillamail.net",
    "guerrillamail.org", "guerrillamail.biz", "guerrillamail.de",
    "tempmail.com", "temp-mail.org", "temp-mail.io",
    "10minutemail.com", "10minutemail.net", "10minutemail.org",
    "yopmail.com", "yopmail.fr", "yopmail.net",
    "throwam.com", "throwam.net",
    "sharklasers.com", "guerrillamailblock.com",
    "grr.la", "spam4.me", "trashmail.com", "trashmail.me",
    "trashmail.net", "trashmail.at", "trashmail.io",
    "fakeinbox.com", "fakeinbox.net",
    "maildrop.cc", "dispostable.com",
    "mailnull.com", "spamgourmet.com",
    "spamgourmet.net", "spamgourmet.org",
    "anonaddy.com", "simplelogin.io",    # Privacy relays — borderline; policy decision
    "33mail.com", "spamex.com",
    "spamfree24.org", "spamhereplease.com",
    "spammotel.com", "spaml.de",
    "spamoff.de", "spamspot.com",
    "spamthis.co.uk", "spamtroll.net",
    "tempinbox.co.uk", "tempinbox.com",
    "tempr.email", "tempsky.com",
    "throwaway.email", "discard.email",
    "mailnesia.com", "mailnull.com",
    "mytemp.email", "owlpic.com",
    "filzmail.com", "trbvm.com",
    "gettempmail.com", "tempemail.net",
    "mohmal.com", "spamwc.de",
    "bspamfree.org", "mt2015.com",
    "mt2014.com", "discard.email",
    "einrot.com", "discardmail.com",
    "discardmail.de", "spamgob.com",
    "humaility.com", "thankyou2010.com",
    "iwi.net", "jetable.com",
    "jetable.fr.nf", "jetable.net",
    "jetable.org", "noref.in",
    "nospam.ze.tc", "obobbo.com",
    "pjjkp.com", "smellfear.com",
    "super-auswahl.de", "toomail.net",
    "tradermail.info", "trash2009.com",
    "trashtmail.com", "tyldd.com",
    "uggsrock.com", "wegwerfmail.de",
    "wegwerfmail.net", "wegwerfmail.org",
    # ── Nigerian-specific disposable providers ────────────────────────────
    # Add as discovered
}
```

```python
# src/utilities/email_validation.py

from src.config.blocked_email_domains import BLOCKED_DOMAINS

def is_disposable_email(email: str) -> bool:
    """Return True if the email domain is a known disposable provider."""
    domain = email.strip().lower().split("@")[-1]
    return domain in BLOCKED_DOMAINS

def validate_email_not_disposable(email: str) -> None:
    """Raise ValueError if the email is from a known disposable provider."""
    if is_disposable_email(email):
        raise ValueError(
            "Temporary or disposable email addresses are not permitted. "
            "Please use a permanent email address to register."
        )
```

---

**Layer 2 — External API check (catches new providers the blocklist hasn't seen)**

Use a third-party disposable email detection API as a second check. Run this asynchronously — only block if the API confidently flags the domain. If the API is unavailable, fall through (do not block — availability takes precedence over the edge case).

Recommended services:
- **Abstract API** (`emailvalidation.abstractapi.com`) — has a Nigerian-friendly free tier
- **Hunter.io email verifier**
- **Kickbox**

```python
# src/services/email_guard.py

import httpx
from src.config.settings import Settings
from src.utilities.email_validation import is_disposable_email

async def is_email_allowed(email: str) -> tuple[bool, str]:
    """
    Returns (is_allowed, reason).
    Checks local blocklist first, then external API if configured.
    """
    # Layer 1: local blocklist (always runs, zero latency)
    if is_disposable_email(email):
        return False, "Disposable email addresses are not permitted."

    # Layer 2: external API (optional, configured via ABSTRACT_API_KEY)
    api_key = getattr(Settings, "ABSTRACT_API_KEY", None)
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    "https://emailvalidation.abstractapi.com/v1/",
                    params={"api_key": api_key, "email": email},
                )
                data = resp.json()
                if data.get("is_disposable_email", {}).get("value") is True:
                    return False, "Disposable email addresses are not permitted."
        except Exception:
            pass  # API unavailable — fall through, do not block

    return True, ""
```

---

**Wire into registration:**

```python
# In auth/register route — before creating the user

allowed, reason = await is_email_allowed(body.email)
if not allowed:
    raise HTTPException(
        status_code=422,
        detail=reason
    )
```

---

**Maintenance:**

- Store the blocklist in `src/config/blocked_email_domains.py` — update via PR whenever a new provider is discovered
- Add an admin endpoint `POST /internal/admin/blocked-domains` to add domains at runtime without deployment (stored in DB table `blocked_email_domain`, merged with the static list at runtime)
- Log every blocked registration attempt to `user_audit_event` with `event_type = 'registration_blocked_disposable_email'` for AML reporting

---

**DB table for runtime-updatable blocklist:**

```sql
CREATE TABLE blocked_email_domain (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain      VARCHAR(255) NOT NULL UNIQUE,
    reason      TEXT,
    -- 'disposable' | 'fraud_history' | 'policy'
    added_by    UUID REFERENCES "user"(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX blocked_email_domain_domain_idx ON blocked_email_domain(domain);
```

This gives you a fast indexed lookup at registration time and an admin UI to manage the list without touching code.

---

### 9.8 Summary Table — Priority Order

| # | Issue | Status | Risk Level | Phase |
|---|---|---|---|---|
| 1 | Failed payout wallet reversal | ❌ MISSING | CRITICAL | Phase 3 |
| 2 | KYC monthly limit race condition | ❌ MISSING | CRITICAL | Phase 3 |
| 3 | Payout approval double-fire | ❌ MISSING | CRITICAL | Phase 3 |
| 4 | Float used for money amounts | ❌ MISSING | HIGH | Phase 1 |
| 5 | Provider-initiated reversal handler | ❌ MISSING | HIGH | Phase 3 |
| 6 | Webhook silent drop on DB error | ❌ MISSING | HIGH | Phase 3 |
| 7 | Platform fee reversal on failure | ❌ MISSING | HIGH | Phase 3 |
| 8 | Stuck run recovery watchdog | ❌ MISSING | HIGH | Phase 3 |
| 9 | Pending candidate requery job | ⚠️ PARTIAL | HIGH | Phase 3 |
| 10 | TOTP replay attack | ❌ MISSING | HIGH | Phase 4 |
| 11 | Rate limiting on auth endpoints | ❌ MISSING | HIGH | Phase 4 |
| 12 | Session invalidation on security change | ❌ MISSING | MEDIUM | Phase 4 |
| 13 | Balance integrity DB constraint | ❌ MISSING | MEDIUM | Phase 1 |
| 14 | No payout reconciliation at run close | ❌ MISSING | MEDIUM | Phase 3 |
| 15 | Dual control on large payouts | ❌ MISSING | MEDIUM | Phase 5 |
| 16 | Concurrent run creation guard | ⚠️ PARTIAL | MEDIUM | Phase 3 |
| 17 | Webhook signature timing attack | ⚠️ PARTIAL | LOW | Phase 4 |
| 18 | Dead letter queue for background tasks | ❌ MISSING | MEDIUM | Phase 4 |
| 19 | Disposable/temporary email rejection | ❌ MISSING | HIGH | Phase 1 |

---

*End of Document*

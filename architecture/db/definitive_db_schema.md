# FlowPilot — Definitive Database Schema (21 Tables)

> **Status:** Final · **Engine:** PostgreSQL 15+ · **ORM:** SQLAlchemy 2.x (Mapped)
>
> This document is the single source of truth for the FlowPilot production database schema. It was synthesized from a 4-agent consensus review (2× Claude Opus 4.6 + 2× GPT-5.3-Codex) that analyzed the live 8-table schema and a proposed 27-table schema, arriving at an optimal 21-table design. The corresponding SQLAlchemy models live in `src/infrastructure/database/flowpilot_models.py`.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Design Rules](#3-design-rules)
4. [Schema by Domain Group](#4-schema-by-domain-group)
   - [Auth & Identity (2)](#41-auth--identity)
   - [Business (3)](#42-business)
   - [Reference Data (1)](#43-reference-data)
   - [Agent Pipeline (3)](#44-agent-pipeline)
   - [Reconciliation (2)](#45-reconciliation)
   - [Risk & Forecast (3)](#46-risk--forecast)
   - [Execution (3)](#47-execution)
   - [Audit & Observability (4)](#48-audit--observability)
5. [Excluded Tables](#5-excluded-tables)
6. [Migration Notes](#6-migration-notes)

---

## 1. Overview

FlowPilot is a fintech agentic-AI platform that automates transaction reconciliation, risk scoring, cash-flow forecasting, and batch payouts via the Interswitch API. The database schema must support:

- **Multi-tenancy** — all domain data scoped by `business_id`
- **Regulatory immutability** — financial records are append-only; no soft deletes on financial data
- **Deterministic arithmetic** — NUMERIC everywhere money or scores are compared
- **High-volume audit** — BIGINT IDENTITY PKs with BRIN indexes for append-only logs
- **Transactional constraint management** — TEXT + CHECK instead of native PG ENUMs

### Consensus Summary

| Dimension | Verdict | Consensus |
|-----------|---------|-----------|
| Enum strategy | TEXT + CHECK (no native PG ENUMs) | 3 of 4 |
| PK strategy | UUID for entities, BIGINT IDENTITY for logs | 4 of 4 |
| Money type | NUMERIC(18,2) | 3 of 4 |
| Score type | NUMERIC(5,4) — never FLOAT | 4 of 4 |
| Multi-tenancy | `business_id` FK on all domain tables | 4 of 4 |
| Anomaly tracking | Separate 1:N table | 4 of 4 |
| Payout batch table | Keep — Interswitch API is batch-based | 4 of 4 |
| Soft deletes on financials | Reject — `is_active` only on reference entities | 4 of 4 |
| Auth | External provider + local `user` table | 3 of 4 |
| Forecast storage | 1 table with JSONB `daily_projections` | 3 of 4 |

---

## 2. Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                        FlowPilot — 21 Tables                        │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  AUTH & IDENTITY (2)          BUSINESS (3)          REFERENCE (1)    │
│  ┌──────────────────┐  ┌──────────────────────┐  ┌───────────────┐  │
│  │ user             │  │ business             │  │ institution   │  │
│  │ business_member  │  │ business_config (1:1)│  │               │  │
│  │                  │  │ invitation           │  │               │  │
│  └────────┬─────────┘  └──────────┬───────────┘  └───────┬───────┘  │
│           │                       │                       │          │
│           └───────────┬───────────┘                       │          │
│                       ▼                                   │          │
│              AGENT PIPELINE (3)                           │          │
│              ┌────────────────────┐                       │          │
│              │ agent_run          │                       │          │
│              │ run_step           │                       │          │
│              │ run_event (BIGINT) │                       │          │
│              └────────┬───────────┘                       │          │
│                       │                                   │          │
│          ┌────────────┼────────────────────┐              │          │
│          ▼            ▼                    ▼              │          │
│  RECONCILIATION (2)  RISK & FORECAST (3)  EXECUTION (3)  │          │
│  ┌────────────────┐  ┌─────────────────┐  ┌────────────────────┐    │
│  │ reconciled_    │  │ payout_candidate│◄─┤ payout_batch       │    │
│  │   transaction  │  │ risk_score_     │  │ customer_lookup_   │    │
│  │ transaction_   │  │   feature (1:1) │  │   result           │    │
│  │   anomaly      │  │ forecast_result │  │ payout_execution   │    │
│  │                │  │                 │  │   (BIGINT)         │    │
│  └────────────────┘  └────────┬────────┘  └────────────────────┘    │
│                               │                                      │
│                               ▼                                      │
│                    AUDIT & OBSERVABILITY (4)                         │
│                    ┌──────────────────────────┐                      │
│                    │ approval_override        │                      │
│                    │ audit_log        (BIGINT)│                      │
│                    │ api_call_log     (BIGINT)│                      │
│                    │ notification_outbox(BIGINT)│                    │
│                    └──────────────────────────┘                      │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Design Rules

| # | Aspect | Rule | Rationale |
|---|--------|------|-----------|
| 1 | **Enums** | `TEXT` + `CHECK` constraint — NO native PG `ENUM` | `ALTER TYPE ADD VALUE` cannot run inside a transaction; removing values requires type recreation. TEXT + CHECK gives transactional constraint swaps. |
| 2 | **Entity PKs** | `UUID DEFAULT gen_random_uuid()` | Globally unique, no coordination across services. |
| 3 | **Log/Append-only PKs** | `BIGINT GENERATED ALWAYS AS IDENTITY` | 8 bytes (vs UUID 16), monotonically increasing for BRIN, optimal for sequential inserts. Used on: `run_event`, `payout_execution`, `audit_log`, `api_call_log`, `notification_outbox`. |
| 4 | **Money columns** | `NUMERIC(18,2)` — never `FLOAT` | Deterministic arithmetic for fintech; `0.35` comparisons must not silently fail from IEEE 754 rounding. |
| 5 | **Score columns** | `NUMERIC(5,4)` — never `FLOAT` | Risk scores gate real money movement; deterministic comparison is non-negotiable. |
| 6 | **Timestamps** | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` | All `created_at` columns. Timezone-aware throughout. |
| 7 | **Soft deletes** | `is_active BOOLEAN DEFAULT true` ONLY on `user`, `business`, `institution` | Financial and audit records are immutable — no soft deletes. |
| 8 | **Multi-tenancy** | `business_id` FK on all domain tables | Enforces per-business data isolation. |
| 9 | **FK on DELETE (children)** | `CASCADE` for run children (`run_step`, `run_event`, etc.) | Deleting a run cascades to all child data. |
| 10 | **FK on DELETE (identity)** | `RESTRICT` for `user`, `business`, `institution` references | Prevent orphaning critical identity records. |
| 11 | **FK on DELETE (optional)** | `SET NULL` for nullable optional FKs (`approved_by`, `step_id`, etc.) | Allow referencing entity deletion without cascading. |
| 12 | **Naming** | Singular `snake_case` for tables and columns | Convention: `payout_candidate` not `payout_candidates`. |
| 13 | **Triggers** | `set_updated_at()` on mutable tables only | Audit/append-only tables have NO `updated_at` column. |
| 14 | **Audit tables** | Immutable — NO `updated_at` column | `approval_override`, `audit_log`, `api_call_log`, `transaction_anomaly`, `risk_score_feature`, `customer_lookup_result`, `payout_execution`, `run_event`. |
| 15 | **JSONB** | Used for semi-structured data (`plan_graph`, `risk_reasons`, `daily_projections`, `preferences`, etc.) | Flexible storage with GIN indexing where queried. |

---

## 4. Schema by Domain Group

### 4.1 Auth & Identity

#### Table 1: `user`

> Local user record. Authentication is delegated to an external provider; `external_id` links to the provider's user identity.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `external_id` | `VARCHAR(255)` | YES | — | `UNIQUE` |
| `email` | `VARCHAR(255)` | NO | — | `UNIQUE` |
| `display_name` | `VARCHAR(100)` | NO | — | |
| `avatar_url` | `VARCHAR(512)` | YES | — | |
| `is_active` | `BOOLEAN` | NO | `true` | |
| `last_login_at` | `TIMESTAMPTZ` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:** None beyond PK and UNIQUE constraints.

**Foreign Keys:** None (root entity).

**CHECK Constraints:** None.

---

#### Table 2: `business_member`

> M:N join between `user` and `business` with an assigned role. A user can belong to multiple businesses.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `business_id` | `UUID` | NO | — | **FK → business.id ON DELETE CASCADE** |
| `user_id` | `UUID` | NO | — | **FK → user.id ON DELETE CASCADE** |
| `role` | `TEXT` | NO | `'analyst'` | CHECK |
| `joined_at` | `TIMESTAMPTZ` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `business_member_business_id_idx` on `(business_id)`
- `business_member_user_id_idx` on `(user_id)`

**Unique Constraints:**
- `business_member_business_user_unique` on `(business_id, user_id)`

**CHECK Constraints:**
- `business_member_role_check`: `role IN ('owner', 'approver', 'analyst')`

---

### 4.2 Business

#### Table 3: `business`

> Multi-tenancy root entity. Every domain record references a business. Stores the Interswitch merchant configuration.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `business_name` | `VARCHAR(255)` | NO | — | |
| `business_type` | `TEXT` | YES | — | |
| `interswitch_merchant_id` | `VARCHAR(128)` | YES | — | |
| `is_active` | `BOOLEAN` | NO | `true` | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `business_is_active_idx` on `(is_active)`

**Foreign Keys:** None (root entity).

**CHECK Constraints:** None.

---

#### Table 4: `business_config`

> 1:1 with `business`. Merges onboarding progress, financial profile, and notification/UI preferences into a single row. Replaces the previously proposed `onboarding_progress`, `business_financial_profiles`, and `notification_preferences` tables.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `business_id` | `UUID` | NO | — | **FK → business.id ON DELETE CASCADE**, `UNIQUE` |
| `onboarding_step` | `TEXT` | NO | `'not_started'` | CHECK |
| `onboarding_completed_at` | `TIMESTAMPTZ` | YES | — | |
| `monthly_txn_volume_range` | `VARCHAR(50)` | YES | — | |
| `avg_monthly_payouts_range` | `VARCHAR(50)` | YES | — | |
| `primary_bank` | `VARCHAR(100)` | YES | — | |
| `primary_use_cases` | `JSONB` | YES | — | |
| `risk_appetite` | `TEXT` | YES | — | CHECK |
| `default_risk_tolerance` | `NUMERIC(5,4)` | NO | `0.3500` | |
| `default_budget_cap` | `NUMERIC(18,2)` | YES | — | |
| `preferences` | `JSONB` | YES | `'{}'` | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:** None beyond PK and UNIQUE constraint on `business_id`.

**CHECK Constraints:**
- `business_config_onboarding_step_check`: `onboarding_step IN ('not_started', 'business_profile', 'financial_setup', 'team_invite', 'complete')`
- `business_config_risk_appetite_check`: `risk_appetite IN ('conservative', 'moderate', 'aggressive')`

---

#### Table 5: `invitation`

> Team invite lifecycle management. Tracks invite tokens, expiry, and acceptance.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `business_id` | `UUID` | NO | — | **FK → business.id ON DELETE CASCADE** |
| `invited_by` | `UUID` | NO | — | **FK → user.id ON DELETE SET NULL** |
| `email` | `VARCHAR(255)` | NO | — | |
| `role` | `TEXT` | NO | — | CHECK |
| `token_hash` | `VARCHAR(255)` | NO | — | `UNIQUE` |
| `status` | `TEXT` | NO | `'pending'` | CHECK |
| `accepted_at` | `TIMESTAMPTZ` | YES | — | |
| `expires_at` | `TIMESTAMPTZ` | NO | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `invitation_business_id_idx` on `(business_id)`
- `invitation_email_idx` on `(email)`
- `invitation_expires_at_idx` on `(expires_at)`

**CHECK Constraints:**
- `invitation_role_check`: `role IN ('approver', 'analyst')`
- `invitation_status_check`: `status IN ('pending', 'accepted', 'expired', 'revoked')`

---

### 4.3 Reference Data

#### Table 6: `institution`

> Cached Interswitch bank/institution code directory. Periodically synced from the Interswitch Institutions API. Soft-deletable via `is_active`.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `institution_code` | `VARCHAR(10)` | NO | — | `UNIQUE` |
| `institution_name` | `VARCHAR(255)` | NO | — | |
| `short_name` | `VARCHAR(50)` | YES | — | |
| `institution_type` | `TEXT` | YES | — | CHECK |
| `is_active` | `BOOLEAN` | NO | `true` | |
| `last_synced_at` | `TIMESTAMPTZ` | YES | — | |
| `raw_response` | `JSONB` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:** None beyond PK and UNIQUE constraint on `institution_code`.

**CHECK Constraints:**
- `institution_type_check`: `institution_type IN ('bank', 'mobile_money', 'microfinance', 'other')`

---

### 4.4 Agent Pipeline

#### Table 7: `agent_run`

> Top-level orchestration record for a single FlowPilot agentic run. Tracks the full lifecycle from planning through execution, including approval gates.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `business_id` | `UUID` | NO | — | **FK → business.id ON DELETE CASCADE** |
| `created_by` | `UUID` | NO | — | **FK → user.id ON DELETE RESTRICT** |
| `objective` | `TEXT` | NO | — | |
| `constraints` | `TEXT` | YES | — | |
| `risk_tolerance` | `NUMERIC(5,4)` | NO | `0.3500` | CHECK |
| `budget_cap` | `NUMERIC(18,2)` | YES | — | CHECK |
| `merchant_id` | `VARCHAR(50)` | NO | — | |
| `date_from` | `DATE` | YES | — | |
| `date_to` | `DATE` | YES | — | |
| `status` | `TEXT` | NO | `'pending'` | CHECK |
| `plan_graph` | `JSONB` | YES | — | |
| `error_message` | `TEXT` | YES | — | |
| `approved_by` | `UUID` | YES | — | **FK → user.id ON DELETE SET NULL** |
| `approved_at` | `TIMESTAMPTZ` | YES | — | |
| `cancelled_by` | `UUID` | YES | — | **FK → user.id ON DELETE SET NULL** |
| `cancelled_at` | `TIMESTAMPTZ` | YES | — | |
| `started_at` | `TIMESTAMPTZ` | YES | — | |
| `completed_at` | `TIMESTAMPTZ` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `agent_run_status_idx` on `(status)`
- `agent_run_business_id_idx` on `(business_id)`
- `agent_run_created_by_idx` on `(created_by)`
- `agent_run_business_id_created_at_idx` on `(business_id, created_at DESC)`

**CHECK Constraints:**
- `agent_run_risk_tolerance_check`: `risk_tolerance >= 0.0000 AND risk_tolerance <= 1.0000`
- `agent_run_budget_cap_check`: `budget_cap >= 0`
- `agent_run_status_check`: `status IN ('pending', 'planning', 'reconciling', 'scoring', 'forecasting', 'awaiting_approval', 'executing', 'completed', 'failed', 'cancelled')`

---

#### Table 8: `run_step`

> Ordered agent steps within a run. Each step corresponds to one agent invocation (planner, reconciliation, risk, forecast, execution, audit).

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `agent_type` | `TEXT` | NO | — | CHECK |
| `step_order` | `SMALLINT` | NO | — | CHECK |
| `description` | `TEXT` | YES | — | |
| `status` | `TEXT` | NO | `'pending'` | CHECK |
| `progress_pct` | `SMALLINT` | YES | — | |
| `input_data` | `JSONB` | YES | — | |
| `output_data` | `JSONB` | YES | — | |
| `error_message` | `TEXT` | YES | — | |
| `started_at` | `TIMESTAMPTZ` | YES | — | |
| `completed_at` | `TIMESTAMPTZ` | YES | — | |
| `duration_ms` | `INTEGER` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `run_step_run_id_idx` on `(run_id)`
- `run_step_run_id_step_order_idx` on `(run_id, step_order)`

**Unique Constraints:**
- `run_step_run_id_step_order_unique` on `(run_id, step_order)`

**CHECK Constraints:**
- `run_step_agent_type_check`: `agent_type IN ('planner', 'reconciliation', 'risk', 'forecast', 'execution', 'audit')`
- `run_step_step_order_check`: `step_order >= 0`
- `run_step_status_check`: `status IN ('pending', 'running', 'completed', 'failed', 'skipped')`

---

#### Table 9: `run_event`

> SSE replay buffer. Append-only event stream for real-time UI updates and post-hoc replay. Uses BIGINT PK for optimal sequential insert performance.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `BIGINT` | NO | `GENERATED ALWAYS AS IDENTITY` | **PK** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `step_id` | `UUID` | YES | — | **FK → run_step.id ON DELETE SET NULL** |
| `event_type` | `VARCHAR(64)` | NO | — | |
| `payload` | `JSONB` | NO | — | |
| `sequence_num` | `INTEGER` | NO | — | |
| `emitted_at` | `TIMESTAMPTZ` | NO | `NOW()` | |

**Indexes:**
- `run_event_run_id_idx` on `(run_id)`
- `run_event_run_id_sequence_num_idx` on `(run_id, sequence_num)`

**Notes:** Immutable — no `updated_at` column, no update trigger.

---

### 4.5 Reconciliation

#### Table 10: `reconciled_transaction`

> Enriched transaction records fetched from the Interswitch Transaction History API and reconciled by the reconciliation agent.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `business_id` | `UUID` | NO | — | **FK → business.id ON DELETE CASCADE** |
| `interswitch_ref` | `VARCHAR(128)` | NO | — | |
| `amount` | `NUMERIC(18,2)` | NO | — | CHECK |
| `currency` | `CHAR(3)` | NO | `'NGN'` | |
| `direction` | `TEXT` | NO | — | CHECK |
| `channel` | `TEXT` | YES | — | CHECK |
| `status` | `TEXT` | NO | — | CHECK |
| `narration` | `TEXT` | YES | — | |
| `transaction_timestamp` | `TIMESTAMPTZ` | YES | — | |
| `settlement_date` | `DATE` | YES | — | |
| `counterparty_name` | `VARCHAR(255)` | YES | — | |
| `counterparty_bank` | `VARCHAR(100)` | YES | — | |
| `has_anomaly` | `BOOLEAN` | NO | `false` | |
| `anomaly_count` | `SMALLINT` | NO | `0` | |
| `raw_payload` | `JSONB` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `reconciled_transaction_run_id_idx` on `(run_id)`
- `reconciled_transaction_business_id_idx` on `(business_id)`
- `reconciled_transaction_interswitch_ref_idx` on `(interswitch_ref)`
- `reconciled_transaction_status_idx` on `(status)`
- `reconciled_transaction_has_anomaly_idx` on `(has_anomaly)`
- `reconciled_transaction_txn_timestamp_idx` on `(transaction_timestamp DESC)`

**Unique Constraints:**
- `reconciled_transaction_run_ref_unique` on `(run_id, interswitch_ref)`

**CHECK Constraints:**
- `reconciled_transaction_amount_check`: `amount >= 0`
- `reconciled_transaction_direction_check`: `direction IN ('inflow', 'outflow')`
- `reconciled_transaction_status_check`: `status IN ('SUCCESS', 'PENDING', 'FAILED', 'REVERSED')`
- `reconciled_transaction_channel_check`: `channel IN ('CARD', 'TRANSFER', 'USSD', 'QR')`

---

#### Table 11: `transaction_anomaly`

> 1:N anomalies per reconciled transaction. Replaces the old boolean `is_anomaly` flag approach, allowing multiple anomaly types per transaction.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `txn_id` | `UUID` | NO | — | **FK → reconciled_transaction.id ON DELETE CASCADE** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `anomaly_type` | `VARCHAR(64)` | NO | — | |
| `severity` | `TEXT` | NO | — | CHECK |
| `description` | `TEXT` | NO | — | |
| `detected_value` | `VARCHAR(255)` | YES | — | |
| `expected_range` | `VARCHAR(255)` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |

**Indexes:**
- `transaction_anomaly_txn_id_idx` on `(txn_id)`
- `transaction_anomaly_run_id_idx` on `(run_id)`
- `transaction_anomaly_type_idx` on `(anomaly_type)`

**CHECK Constraints:**
- `transaction_anomaly_severity_check`: `severity IN ('low', 'medium', 'high')`

**Notes:** Immutable — no `updated_at` column.

---

### 4.6 Risk & Forecast

#### Table 12: `payout_candidate`

> Central entity for the payout lifecycle. Progressively enriched through risk scoring → customer lookup → approval → execution. Links to `institution` by code for bank resolution.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `business_id` | `UUID` | NO | — | **FK → business.id ON DELETE CASCADE** |
| `batch_id` | `UUID` | YES | — | **FK → payout_batch.id ON DELETE SET NULL** |
| `institution_code` | `VARCHAR(10)` | NO | — | **FK → institution.institution_code ON DELETE RESTRICT** |
| `beneficiary_name` | `VARCHAR(255)` | NO | — | |
| `account_number` | `VARCHAR(20)` | NO | — | |
| `amount` | `NUMERIC(18,2)` | NO | — | CHECK |
| `currency` | `CHAR(3)` | NO | `'NGN'` | |
| `purpose` | `VARCHAR(255)` | YES | — | |
| `risk_score` | `NUMERIC(5,4)` | YES | — | CHECK |
| `risk_reasons` | `JSONB` | NO | `'[]'` | |
| `risk_decision` | `TEXT` | YES | — | CHECK |
| `lookup_status` | `TEXT` | NO | `'pending'` | CHECK |
| `lookup_account_name` | `VARCHAR(255)` | YES | — | |
| `lookup_match_score` | `NUMERIC(5,4)` | YES | — | |
| `approval_status` | `TEXT` | NO | `'pending'` | CHECK |
| `approved_by` | `UUID` | YES | — | **FK → user.id ON DELETE SET NULL** |
| `approved_at` | `TIMESTAMPTZ` | YES | — | |
| `execution_status` | `TEXT` | NO | `'not_started'` | CHECK |
| `client_reference` | `VARCHAR(100)` | YES | — | `UNIQUE` |
| `provider_reference` | `VARCHAR(100)` | YES | — | |
| `executed_at` | `TIMESTAMPTZ` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `payout_candidate_run_id_idx` on `(run_id)`
- `payout_candidate_business_id_idx` on `(business_id)`
- `payout_candidate_run_id_risk_decision_idx` on `(run_id, risk_decision)`
- `payout_candidate_run_id_approval_status_idx` on `(run_id, approval_status)`
- `payout_candidate_institution_code_idx` on `(institution_code)`
- `payout_candidate_batch_id_idx` on `(batch_id)`
- `payout_candidate_approved_by_idx` on `(approved_by)`
- `payout_candidate_risk_reasons_idx` on `(risk_reasons)` using **GIN**

**CHECK Constraints:**
- `payout_candidate_amount_check`: `amount > 0`
- `payout_candidate_risk_score_check`: `risk_score >= 0.0000 AND risk_score <= 1.0000`
- `payout_candidate_risk_decision_check`: `risk_decision IN ('allow', 'review', 'block')`
- `payout_candidate_lookup_status_check`: `lookup_status IN ('pending', 'success', 'failed', 'mismatch')`
- `payout_candidate_approval_status_check`: `approval_status IN ('pending', 'approved', 'rejected')`
- `payout_candidate_execution_status_check`: `execution_status IN ('not_started', 'pending', 'success', 'failed', 'requires_followup')`

---

#### Table 13: `risk_score_feature`

> 1:1 explainability record for a payout candidate's risk score. Stores the individual features used by the risk model, enabling compliance audits and model debugging.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `candidate_id` | `UUID` | NO | — | **FK → payout_candidate.id ON DELETE CASCADE**, `UNIQUE` |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `historical_frequency` | `INTEGER` | YES | — | |
| `amount_deviation_ratio` | `NUMERIC(8,4)` | YES | — | |
| `avg_historical_amount` | `NUMERIC(18,2)` | YES | — | |
| `duplicate_similarity_score` | `NUMERIC(5,4)` | YES | — | |
| `lookup_mismatch_flag` | `BOOLEAN` | YES | — | |
| `account_anomaly_count` | `SMALLINT` | YES | — | |
| `account_age_days` | `INTEGER` | YES | — | |
| `days_since_last_payout` | `INTEGER` | YES | — | |
| `amount_vs_budget_cap_pct` | `NUMERIC(7,4)` | YES | — | |
| `model_version` | `VARCHAR(32)` | NO | — | |
| `computed_at` | `TIMESTAMPTZ` | NO | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |

**Indexes:**
- `risk_score_feature_run_id_idx` on `(run_id)`

**Notes:** Immutable — no `updated_at` column.

---

#### Table 14: `forecast_result`

> Per-run cash-flow forecast. Contains balance projections and feasibility assessment. `daily_projections` stored as JSONB array to avoid a separate table (consensus: 3 of 4 reviewers).

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE**, `UNIQUE` |
| `business_id` | `UUID` | NO | — | **FK → business.id ON DELETE CASCADE** |
| `balance_today` | `NUMERIC(18,2)` | NO | — | |
| `total_payout_batch` | `NUMERIC(18,2)` | NO | — | |
| `projected_balance_after` | `NUMERIC(18,2)` | NO | — | |
| `feasibility` | `TEXT` | NO | — | CHECK |
| `stress_flag` | `BOOLEAN` | NO | `false` | |
| `inflow_7d_projected` | `NUMERIC(18,2)` | YES | — | |
| `outflow_7d_projected` | `NUMERIC(18,2)` | YES | — | |
| `net_7d_projected` | `NUMERIC(18,2)` | YES | — | |
| `daily_projections` | `JSONB` | YES | — | |
| `model_version` | `VARCHAR(32)` | NO | — | |
| `computed_at` | `TIMESTAMPTZ` | NO | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `forecast_result_business_id_idx` on `(business_id)`

**CHECK Constraints:**
- `forecast_result_feasibility_check`: `feasibility IN ('safe', 'caution', 'block')`

---

### 4.7 Execution

#### Table 15: `payout_batch`

> Tracks Interswitch batch payout submissions. The Interswitch Payouts API is batch-based — `batchReference`, `submissionStatus`, `acceptedCount`, and `rejectedCount` need a dedicated table (unanimous consensus).

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `business_id` | `UUID` | NO | — | **FK → business.id ON DELETE CASCADE** |
| `batch_reference` | `VARCHAR(100)` | NO | — | `UNIQUE` |
| `currency` | `CHAR(3)` | NO | `'NGN'` | |
| `source_account_id` | `VARCHAR(100)` | NO | — | |
| `total_amount` | `NUMERIC(18,2)` | NO | — | CHECK |
| `item_count` | `SMALLINT` | NO | — | CHECK |
| `accepted_count` | `SMALLINT` | NO | `0` | |
| `rejected_count` | `SMALLINT` | NO | `0` | |
| `submission_status` | `TEXT` | NO | `'pending'` | CHECK |
| `submitted_at` | `TIMESTAMPTZ` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `payout_batch_run_id_idx` on `(run_id)`
- `payout_batch_business_id_idx` on `(business_id)`

**CHECK Constraints:**
- `payout_batch_total_amount_check`: `total_amount >= 0`
- `payout_batch_item_count_check`: `item_count > 0`
- `payout_batch_submission_status_check`: `submission_status IN ('pending', 'accepted', 'partial', 'rejected', 'failed')`

---

#### Table 16: `customer_lookup_result`

> Per-lookup API call detail for Interswitch Customer Validation. Each attempt is recorded for auditability.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `candidate_id` | `UUID` | NO | — | **FK → payout_candidate.id ON DELETE CASCADE** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `account_number` | `VARCHAR(20)` | NO | — | |
| `institution_code` | `VARCHAR(10)` | NO | — | |
| `name_returned` | `VARCHAR(255)` | YES | — | |
| `similarity_score` | `NUMERIC(5,4)` | YES | — | |
| `http_status_code` | `SMALLINT` | NO | — | |
| `response_message` | `TEXT` | YES | — | |
| `raw_response` | `JSONB` | NO | — | |
| `attempt_number` | `SMALLINT` | NO | `1` | |
| `duration_ms` | `INTEGER` | NO | — | |
| `called_at` | `TIMESTAMPTZ` | NO | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |

**Indexes:**
- `customer_lookup_result_candidate_id_idx` on `(candidate_id)`
- `customer_lookup_result_run_id_idx` on `(run_id)`

**Notes:** Immutable — no `updated_at` column.

---

#### Table 17: `payout_execution`

> Per-submission/poll API call detail for Interswitch Payout execution. Append-only log with BIGINT PK for optimal insert performance and BRIN indexing.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `BIGINT` | NO | `GENERATED ALWAYS AS IDENTITY` | **PK** |
| `candidate_id` | `UUID` | NO | — | **FK → payout_candidate.id ON DELETE CASCADE** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `submission_type` | `TEXT` | NO | — | CHECK |
| `interswitch_reference` | `VARCHAR(128)` | YES | — | |
| `http_status_code` | `SMALLINT` | NO | — | |
| `response_message` | `TEXT` | YES | — | |
| `execution_status` | `TEXT` | NO | — | CHECK |
| `raw_response` | `JSONB` | NO | — | |
| `attempt_number` | `SMALLINT` | NO | `1` | |
| `duration_ms` | `INTEGER` | NO | — | |
| `called_at` | `TIMESTAMPTZ` | NO | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |

**Indexes:**
- `payout_execution_candidate_id_idx` on `(candidate_id)`
- `payout_execution_run_id_idx` on `(run_id)`
- `payout_execution_interswitch_ref_idx` on `(interswitch_reference)` — **partial**, `WHERE interswitch_reference IS NOT NULL`
- `payout_execution_called_at_idx` on `(called_at)` using **BRIN**

**CHECK Constraints:**
- `payout_execution_submission_type_check`: `submission_type IN ('submission', 'status_poll')`
- `payout_execution_status_check`: `execution_status IN ('pending', 'success', 'failed', 'requires_followup')`

**Notes:** Immutable — no `updated_at` column.

---

### 4.8 Audit & Observability

#### Table 18: `approval_override`

> Immutable audit trail for when a human overrides a risk agent's decision on a payout candidate. Records both the original and new decision for compliance.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `UUID` | NO | `gen_random_uuid()` | **PK** |
| `candidate_id` | `UUID` | NO | — | **FK → payout_candidate.id ON DELETE CASCADE** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `overridden_by` | `UUID` | NO | — | **FK → user.id ON DELETE SET NULL** |
| `original_decision` | `TEXT` | NO | — | CHECK |
| `original_score` | `NUMERIC(5,4)` | NO | — | |
| `new_decision` | `TEXT` | NO | — | CHECK |
| `reason` | `TEXT` | NO | — | |
| `overridden_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |

**Indexes:**
- `approval_override_candidate_id_idx` on `(candidate_id)`
- `approval_override_run_id_idx` on `(run_id)`
- `approval_override_overridden_by_idx` on `(overridden_by)`

**CHECK Constraints:**
- `approval_override_original_decision_check`: `original_decision IN ('allow', 'review', 'block')`
- `approval_override_new_decision_check`: `new_decision IN ('allow', 'review', 'block')`

**Notes:** Immutable — no `updated_at` column.

---

#### Table 19: `audit_log`

> Agent action trail. Every significant agent action (API calls, decisions, state transitions) is logged here. BIGINT PK with BRIN index on `created_at` for efficient time-range queries over high-volume data.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `BIGINT` | NO | `GENERATED ALWAYS AS IDENTITY` | **PK** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `step_id` | `UUID` | YES | — | **FK → run_step.id ON DELETE SET NULL** |
| `agent_type` | `TEXT` | YES | — | CHECK |
| `action` | `VARCHAR(64)` | NO | — | |
| `detail` | `JSONB` | YES | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |

**Indexes:**
- `audit_log_run_id_idx` on `(run_id)`
- `audit_log_run_id_created_at_idx` on `(run_id, created_at)`
- `audit_log_step_id_idx` on `(step_id)`
- `audit_log_created_at_idx` on `(created_at)` using **BRIN**
- `audit_log_detail_idx` on `(detail)` using **GIN**

**CHECK Constraints:**
- `audit_log_agent_type_check`: `agent_type IN ('planner', 'reconciliation', 'risk', 'forecast', 'execution', 'audit')`

**Notes:** Immutable — no `updated_at` column.

---

#### Table 20: `api_call_log`

> Interswitch API call trace. Separated from `audit_log` (consensus: 3 of 4) to capture HTTP-level details (method, status, duration, request/response sizes) without bloating the general audit trail.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `BIGINT` | NO | `GENERATED ALWAYS AS IDENTITY` | **PK** |
| `run_id` | `UUID` | NO | — | **FK → agent_run.id ON DELETE CASCADE** |
| `step_id` | `UUID` | YES | — | **FK → run_step.id ON DELETE SET NULL** |
| `agent_type` | `TEXT` | NO | — | CHECK |
| `endpoint` | `VARCHAR(255)` | NO | — | |
| `http_method` | `VARCHAR(8)` | NO | — | |
| `http_status_code` | `SMALLINT` | NO | — | |
| `duration_ms` | `INTEGER` | NO | — | |
| `request_size_bytes` | `INTEGER` | YES | — | |
| `response_size_bytes` | `INTEGER` | YES | — | |
| `error_code` | `VARCHAR(64)` | YES | — | |
| `error_message` | `TEXT` | YES | — | |
| `called_at` | `TIMESTAMPTZ` | NO | — | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |

**Indexes:**
- `api_call_log_run_id_idx` on `(run_id)`
- `api_call_log_step_id_idx` on `(step_id)`
- `api_call_log_called_at_idx` on `(called_at)` using **BRIN**

**CHECK Constraints:**
- `api_call_log_agent_type_check`: `agent_type IN ('planner', 'reconciliation', 'risk', 'forecast', 'execution', 'audit')`

**Notes:** Immutable — no `updated_at` column.

---

#### Table 21: `notification_outbox`

> Transactional outbox for reliable async notification delivery. Supports email, in-app, and WhatsApp channels. The outbox pattern guarantees at-least-once delivery even if the notification service is temporarily unavailable.

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | `BIGINT` | NO | `GENERATED ALWAYS AS IDENTITY` | **PK** |
| `user_id` | `UUID` | NO | — | **FK → user.id ON DELETE CASCADE** |
| `business_id` | `UUID` | YES | — | **FK → business.id ON DELETE CASCADE** |
| `run_id` | `UUID` | YES | — | **FK → agent_run.id ON DELETE SET NULL** |
| `notification_type` | `TEXT` | NO | — | |
| `channel` | `TEXT` | NO | — | CHECK |
| `subject` | `VARCHAR(255)` | YES | — | |
| `body` | `TEXT` | NO | — | |
| `extra_data` | `JSONB` | YES | — | |
| `is_sent` | `BOOLEAN` | NO | `false` | |
| `sent_at` | `TIMESTAMPTZ` | YES | — | |
| `send_attempts` | `SMALLINT` | NO | `0` | |
| `last_attempt_at` | `TIMESTAMPTZ` | YES | — | |
| `error_message` | `TEXT` | YES | — | |
| `scheduled_for` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `created_at` | `TIMESTAMPTZ` | NO | `NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | NO | `NOW()` | `set_updated_at()` trigger |

**Indexes:**
- `notification_outbox_user_id_idx` on `(user_id)`
- `notification_outbox_business_id_idx` on `(business_id)`
- `notification_outbox_unsent_idx` on `(is_sent)` — **partial**, `WHERE is_sent = false`
- `notification_outbox_scheduled_for_idx` on `(scheduled_for)`

**CHECK Constraints:**
- `notification_outbox_channel_check`: `channel IN ('email', 'in_app', 'whatsapp')`

**Notes:** `notification_outbox` is the only BIGINT-PK table with an `updated_at` column because delivery attempts mutate the row (`send_attempts`, `is_sent`, `error_message`).

---

## 5. Excluded Tables

The following tables from the proposed 27-table schema were excluded by consensus:

| Excluded Table | Reason | Consensus |
|----------------|--------|-----------|
| `email_verification` | Delegated to the external auth provider | 3 of 4 |
| `password_reset` | Auth provider handles credential management | 3 of 4 |
| `user_session` | Auth provider manages session lifecycle | 3 of 4 |
| `onboarding_progress` | Merged into `business_config` — single 1:1 table | 4 of 4 |
| `business_financial_profiles` | Merged into `business_config` | 4 of 4 |
| `notification_preferences` | Stored as JSONB in `business_config.preferences` | 3 of 4 |
| `forecast_projection` | Stored as JSONB array in `forecast_result.daily_projections` | 3 of 4 |
| `user_activity_log` | Deferred to Phase 2 — not needed for MVP | 3 of 4 |
| `audit_report` | Generated artifact, not an OLTP table | 2 of 4 |

---

## 6. Migration Notes

### From Current Schema (8 Tables) → Definitive (21 Tables)

1. **Rename `operator` → `user`**
   - Add `external_id`, `avatar_url`, `is_active` columns
   - A backward-compatibility alias `OperatorModel = UserModel` exists in the ORM

2. **Rename `plan_step` → `run_step`**
   - Alias `PlanStepModel = RunStepModel` provided for imports

3. **Rename `transaction` → `reconciled_transaction`**
   - Alias `TransactionModel = ReconciledTransactionModel` provided
   - Add `business_id`, `channel`, `counterparty_*`, `anomaly_count` columns
   - Convert `is_anomaly` boolean queries to JOIN against `transaction_anomaly`

4. **Add `business_id` FK** to all domain tables for multi-tenancy

5. **Convert FLOAT → NUMERIC(5,4)** for `risk_score` columns
   - Zero-downtime: add new NUMERIC column, backfill via `CAST(old_col AS NUMERIC(5,4))`, swap columns

6. **Convert NUMERIC(15,2) → NUMERIC(18,2)** for money columns
   - Safe: `ALTER COLUMN ... TYPE NUMERIC(18,2)` is a metadata-only change (widening precision)

7. **New tables** (13 added):
   - `business_member`, `business_config`, `invitation`
   - `institution` (enhanced with `institution_type`, `raw_response`)
   - `run_event`
   - `transaction_anomaly`
   - `risk_score_feature`, `forecast_result`
   - `payout_batch` (already existed, retained)
   - `customer_lookup_result`, `payout_execution`
   - `approval_override`, `api_call_log`, `notification_outbox`

8. **Enum migration**: Replace any existing native PG ENUMs with TEXT + CHECK constraints
   - Use `ALTER TABLE ... DROP CONSTRAINT ... ; ALTER TABLE ... ADD CONSTRAINT ...` inside a single transaction

### Alembic Considerations

- All CHECK constraint changes are transactional (unlike `ALTER TYPE ADD VALUE` for native ENUMs)
- Use `op.create_check_constraint()` / `op.drop_constraint()` for enum value changes
- `set_updated_at()` trigger must be created in the initial migration and applied to all mutable tables
- BRIN indexes require `CREATE INDEX ... USING BRIN` — supported in Alembic via `postgresql_using='brin'`

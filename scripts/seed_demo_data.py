#!/usr/bin/env python3
"""
Strategic demo data seeder for FlowPilot.

Seeds a realistic 2-week history of Racon Labs using FlowPilot
for payroll, vendor payments, and bonus disbursements.

Usage:
    conda activate datathon
    python scripts/seed_demo_data.py

Idempotent: uses ON CONFLICT DO NOTHING for all inserts.
"""

import os
import sys
import uuid
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2  # sync driver for simple seeding

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env manually (avoid dependency on python-dotenv)
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

# ---------------------------------------------------------------------------
# Known IDs (from existing DB data)
# ---------------------------------------------------------------------------
USER_SYSTEM = "00000000-0000-0000-0000-000000000001"
USER_ABDULRAQIB = "d00caff0-d0cc-4735-b753-4ff82b39ae14"
BIZ_RACON = "47f1a700-e4d7-4469-8fbc-87e9e95757fc"

# Fixed UUIDs for reproducibility
RUN_1 = "a1000001-0001-4000-8000-000000000001"  # payroll — completed
RUN_2 = "a1000002-0002-4000-8000-000000000002"  # vendors — completed_with_errors
RUN_3 = "a1000003-0003-4000-8000-000000000003"  # bonuses — awaiting_approval
RUN_4 = "a1000004-0004-4000-8000-000000000004"  # contractors — pending

# Timestamps anchored to the Nigerian demo timeline
TZ = timezone(timedelta(hours=1))  # WAT
T_BASE = datetime(2026, 2, 20, 9, 0, 0, tzinfo=TZ)

def t(days=0, hours=0, minutes=0):
    """Offset from base timestamp."""
    return T_BASE + timedelta(days=days, hours=hours, minutes=minutes)

def uid(name: str = ""):
    """Deterministic UUID based on name, or random if no name given."""
    if name:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"flowpilot.seed.{name}"))
    return str(uuid.uuid4())

# ---------------------------------------------------------------------------
# Run 1: February Payroll (COMPLETED)
# ---------------------------------------------------------------------------
run1_steps = []
run1_candidates = []
run1_transactions = []
run1_audit = []
run1_api_calls = []
run1_events = []
run1_lookups = []
run1_risk_features = []
run1_executions = []

# Step IDs
r1s_plan = uid("r1.step.plan")
r1s_recon = uid("r1.step.recon")
r1s_risk = uid("r1.step.risk")
r1s_exec = uid("r1.step.exec")
r1s_audit = uid("r1.step.audit")

run1_steps = [
    (r1s_plan, RUN_1, "planner", 0, "Decompose payroll objective into execution plan",
     "completed", 100, None,
     json.dumps({"plan_steps": [
         {"step_id": "step_1", "agent_type": "reconciliation", "order": 1, "description": "Pull recent transactions for Racon Labs"},
         {"step_id": "step_2", "agent_type": "risk", "order": 2, "description": "Score payroll candidates for risk"},
         {"step_id": "step_3", "agent_type": "execution", "order": 3, "description": "Verify and execute approved payouts"},
         {"step_id": "step_4", "agent_type": "audit", "order": 4, "description": "Generate compliance audit trail"},
     ], "summary": "4-step payroll disbursement plan"}),
     None, t(0, 0, 0), t(0, 0, 0, ), 2340, t(0, 0, 0)),
    (r1s_recon, RUN_1, "reconciliation", 1, "Pull and reconcile recent transaction history",
     "completed", 100, None,
     json.dumps({"total_fetched": 8, "ledger": {"total_inflow": 4250000, "success_count": 6, "pending_count": 1, "failed_count": 1}}),
     None, t(0, 0, 3), t(0, 0, 6), 2870, t(0, 0, 3)),
    (r1s_risk, RUN_1, "risk", 2, "Score 6 payroll candidates for risk",
     "completed", 100, None,
     json.dumps({"total_scored": 6, "allow": 5, "review": 1, "block": 0}),
     None, t(0, 0, 7), t(0, 0, 9), 1950, t(0, 0, 7)),
    (r1s_exec, RUN_1, "execution", 3, "Verify beneficiaries and execute payouts",
     "completed", 100, None,
     json.dumps({"verified": 6, "executed": 6, "success": 6, "failed": 0}),
     None, t(0, 0, 10), t(0, 0, 14), 4120, t(0, 0, 10)),
    (r1s_audit, RUN_1, "audit", 4, "Generate audit trail and compliance report",
     "completed", 100, None,
     json.dumps({"entries_logged": 12, "report_generated": True}),
     None, t(0, 0, 15), t(0, 0, 16), 1680, t(0, 0, 15)),
]

# 6 payroll employees
_payroll = [
    ("Adewale Okafor",      "058", "0123456789", 350000.00, 0.08, "allow"),
    ("Chidinma Nwosu",      "044", "2098765432", 420000.00, 0.05, "allow"),
    ("Abdullahi Bello",     "011", "3012345678", 280000.00, 0.12, "allow"),
    ("Oluwaseun Adeyemi",   "057", "1234567890", 500000.00, 0.15, "allow"),
    ("Ngozi Eze",           "033", "4056789012", 310000.00, 0.22, "allow"),
    ("Ibrahim Musa",        "070", "5067890123", 195000.00, 0.34, "review"),
]

r1_batch_id = uid("r1.batch")
for i, (name, bank, acct, amount, risk, decision) in enumerate(_payroll):
    cid = uid(f"r1.cand.{i}")
    run1_candidates.append((
        cid, RUN_1, BIZ_RACON, name, bank, acct, amount,
        risk, json.dumps(["payroll_regular"] if risk < 0.3 else ["amount_near_threshold", "new_account_90d"]),
        decision, "approved", USER_ABDULRAQIB, t(0, 0, 10),
        "success", f"FP_{RUN_1[:8]}_{cid[:8]}",
        f"ISW-20260220-{1000+i}", t(0, 0, 12+i), r1_batch_id,
    ))
    # Customer lookup
    run1_lookups.append((
        uid(f"r1.lookup.{i}"), cid, RUN_1, acct, bank, name, 0.95 + (i * 0.005),
        200, "Account validated successfully",
        json.dumps({"accountName": name, "accountNumber": acct, "bankCode": bank, "canCredit": True}),
        1, 280 + i * 30, t(0, 0, 10, ), f"FP_{RUN_1[:8]}_{cid[:8]}", True,
    ))
    # Risk features
    run1_risk_features.append((
        uid(f"r1.risk.{i}"), cid, RUN_1, 3 + i, round(amount / 350000, 4), 320000.00,
        0.0, False, 0, 180 + i * 30, 28 + i * 5, round(amount / 2000000, 4),
        "v1.0-llama3.3", t(0, 0, 8),
    ))
    # Payout execution
    run1_executions.append((
        RUN_1, cid, "success", f"ISW-20260220-{1000+i}",
        "submission", 200, json.dumps({"status": "Successful", "responseCode": "00"}),
        450 + i * 20, t(0, 0, 12 + i),
    ))

# 8 reconciled transactions (inflows from customers before payroll)
_txns = [
    ("ISW-20260219-0001", 850000.00, "inflow", "SUCCESS", "TRANSFER", "TechCorp Nigeria", "GTBank", t(-1, 10, 0)),
    ("ISW-20260219-0002", 1200000.00, "inflow", "SUCCESS", "TRANSFER", "Paystack Settlement", "Access Bank", t(-1, 11, 0)),
    ("ISW-20260219-0003", 450000.00, "inflow", "SUCCESS", "CARD", "Flutterwave Payout", "First Bank", t(-1, 14, 0)),
    ("ISW-20260219-0004", 350000.00, "inflow", "SUCCESS", "TRANSFER", "MTN API Revenue", "Zenith Bank", t(-1, 15, 30)),
    ("ISW-20260219-0005", 680000.00, "inflow", "SUCCESS", "TRANSFER", "Kuda MFB Sweep", "UBA", t(-1, 16, 0)),
    ("ISW-20260219-0006", 720000.00, "inflow", "SUCCESS", "TRANSFER", "Moniepoint Settlement", "Stanbic IBTC", t(-1, 9, 45)),
    ("ISW-20260219-0007", 180000.00, "inflow", "PENDING", "USSD", "USSD Collection", "Wema Bank", t(-1, 17, 0)),
    ("ISW-20260219-0008", 95000.00,  "inflow", "FAILED",  "CARD", "Card Payment Failed", "Fidelity Bank", t(-1, 12, 0)),
]
for i, (ref, amt, direction, status, channel, cpty, bank, ts) in enumerate(_txns):
    run1_transactions.append((
        uid(f"r1.txn.{i}"), RUN_1, BIZ_RACON, ref, amt, "NGN", direction, channel,
        status, f"Payment from {cpty}", ts,
        ts.date() if status == "SUCCESS" else None,
        cpty, bank,
        status == "FAILED",  # has_anomaly
        1 if status == "FAILED" else 0,
    ))

# Audit log entries for Run 1
_run1_audit_actions = [
    (r1s_plan, "planner", "plan_generated", {"step_count": 4, "summary": "4-step payroll disbursement plan"}, t(0, 0, 0)),
    (r1s_recon, "reconciliation", "quick_search_complete", {"merchant_id": "MX272008", "total_fetched": 8}, t(0, 0, 4)),
    (r1s_recon, "reconciliation", "reconciliation_complete", {"total_transactions": 8, "unresolved_count": 1, "resolved_count": 0}, t(0, 0, 6)),
    (r1s_risk, "risk", "risk_scoring_complete", {"total_scored": 6, "allow": 5, "review": 1, "block": 0}, t(0, 0, 9)),
    (r1s_exec, "execution", "beneficiary_verification_complete", {"verified": 6, "name_match_failures": 0}, t(0, 0, 11)),
    (r1s_exec, "execution", "payout_batch_submitted", {"batch_ref": f"FP-BATCH-{RUN_1[:8]}", "total_amount": 2055000, "item_count": 6}, t(0, 0, 12)),
    (r1s_exec, "execution", "payout_execution_complete", {"success": 6, "failed": 0, "pending": 0}, t(0, 0, 14)),
    (r1s_audit, "audit", "audit_report_generated", {"entries_count": 7, "anomalies_flagged": 1}, t(0, 0, 16)),
]
for step_id, agent, action, detail, ts in _run1_audit_actions:
    run1_audit.append((RUN_1, step_id, agent, action, json.dumps(detail), ts))

# API call logs for Run 1
_run1_apis = [
    (r1s_plan, "planner", "https://api.groq.com/openai/v1/chat/completions", "POST", 200, 2340, t(0, 0, 0)),
    (r1s_recon, "reconciliation", "/transaction-search/quick-search", "POST", 200, 1850, t(0, 0, 3)),
    (r1s_recon, "reconciliation", "/transaction-search/reference-search", "POST", 200, 620, t(0, 0, 5)),
    (r1s_risk, "risk", "https://api.groq.com/openai/v1/chat/completions", "POST", 200, 1950, t(0, 0, 7)),
    (r1s_exec, "execution", "/payouts/api/v1/customers/lookup", "POST", 200, 340, t(0, 0, 10)),
    (r1s_exec, "execution", "/payouts/api/v1/payouts", "POST", 200, 780, t(0, 0, 12)),
    (r1s_audit, "audit", "https://api.groq.com/openai/v1/chat/completions", "POST", 200, 1680, t(0, 0, 15)),
]
for step_id, agent, endpoint, method, status, dur, ts in _run1_apis:
    run1_api_calls.append((RUN_1, step_id, agent, endpoint, method, status, dur, ts))

# Run events for Run 1
_run1_events = [
    ("run_created", {"objective": "Process February salary payroll for 6 employees"}, t(0, 0, 0)),
    ("step_started", {"step": "plan", "agent": "planner"}, t(0, 0, 0)),
    ("step_completed", {"step": "plan"}, t(0, 0, 2)),
    ("step_started", {"step": "reconcile", "agent": "reconciliation"}, t(0, 0, 3)),
    ("step_completed", {"step": "reconcile", "txn_count": 8}, t(0, 0, 6)),
    ("step_started", {"step": "risk", "agent": "risk"}, t(0, 0, 7)),
    ("step_completed", {"step": "risk", "allow": 5, "review": 1}, t(0, 0, 9)),
    ("approval_granted", {"approved_by": USER_ABDULRAQIB, "candidates_approved": 6}, t(0, 0, 10)),
    ("step_started", {"step": "execute", "agent": "execution"}, t(0, 0, 10)),
    ("step_completed", {"step": "execute", "success": 6}, t(0, 0, 14)),
    ("step_started", {"step": "audit", "agent": "audit"}, t(0, 0, 15)),
    ("step_completed", {"step": "audit"}, t(0, 0, 16)),
    ("run_completed", {"status": "completed", "total_disbursed": 2055000}, t(0, 0, 16)),
]
for i, (etype, payload, ts) in enumerate(_run1_events):
    run1_events.append((RUN_1, None, etype, json.dumps(payload), i + 1, ts))

# ---------------------------------------------------------------------------
# Run 2: Vendor Invoices (COMPLETED_WITH_ERRORS)
# ---------------------------------------------------------------------------
r2s_plan = uid("r2.step.plan")
r2s_recon = uid("r2.step.recon")
r2s_risk = uid("r2.step.risk")
r2s_exec = uid("r2.step.exec")
r2s_audit = uid("r2.step.audit")

run2_steps = [
    (r2s_plan, RUN_2, "planner", 0, "Plan vendor payment execution",
     "completed", 100, None,
     json.dumps({"plan_steps": [
         {"step_id": "s1", "agent_type": "reconciliation", "order": 1, "description": "Reconcile vendor ledger"},
         {"step_id": "s2", "agent_type": "risk", "order": 2, "description": "Score vendor payouts"},
         {"step_id": "s3", "agent_type": "execution", "order": 3, "description": "Execute vendor payments"},
         {"step_id": "s4", "agent_type": "audit", "order": 4, "description": "Audit trail"},
     ]}),
     None, t(5, 10, 0), t(5, 10, 3), 2780, t(5, 10, 0)),
    (r2s_recon, RUN_2, "reconciliation", 1, "Reconcile vendor payment history",
     "completed", 100, None,
     json.dumps({"total_fetched": 5, "ledger": {"total_inflow": 3800000, "success_count": 4, "pending_count": 0, "failed_count": 1}}),
     None, t(5, 10, 4), t(5, 10, 7), 2200, t(5, 10, 4)),
    (r2s_risk, RUN_2, "risk", 2, "Score 3 vendor candidates",
     "completed", 100, None,
     json.dumps({"total_scored": 3, "allow": 1, "review": 1, "block": 1}),
     None, t(5, 10, 8), t(5, 10, 10), 2100, t(5, 10, 8)),
    (r2s_exec, RUN_2, "execution", 3, "Execute approved vendor payouts",
     "completed", 100, None,
     json.dumps({"verified": 2, "executed": 2, "success": 2, "failed": 0, "skipped_blocked": 1}),
     None, t(5, 10, 11), t(5, 10, 15), 3800, t(5, 10, 11)),
    (r2s_audit, RUN_2, "audit", 4, "Vendor payment audit report",
     "completed", 100, None,
     json.dumps({"entries_logged": 10, "report_generated": True, "blocked_candidates": 1}),
     None, t(5, 10, 16), t(5, 10, 17), 1520, t(5, 10, 16)),
]

r2_batch_id = uid("r2.batch")
_vendors = [
    ("Dangote Cement PLC",  "011", "7012345678", 1800000.00, 0.12, "allow",  "approved", "success"),
    ("MTN Nigeria Comms",   "057", "8023456789", 950000.00,  0.38, "review", "approved", "success"),
    ("Multichoice (DSTV)",  "058", "9034567890", 2200000.00, 0.72, "block",  "rejected", "not_started"),
]
run2_candidates = []
run2_lookups = []
run2_risk_features = []
run2_executions = []
for i, (name, bank, acct, amount, risk, decision, approval, exec_st) in enumerate(_vendors):
    cid = uid(f"r2.cand.{i}")
    run2_candidates.append((
        cid, RUN_2, BIZ_RACON, name, bank, acct, amount,
        risk,
        json.dumps(["vendor_regular"] if risk < 0.3 else (
            ["amount_spike_2x", "first_time_vendor"] if risk < 0.6 else
            ["amount_spike_5x", "duplicate_reference_detected", "vendor_not_verified"]
        )),
        decision, approval,
        USER_ABDULRAQIB if approval == "approved" else None,
        t(5, 10, 11) if approval == "approved" else None,
        exec_st,
        f"FP_{RUN_2[:8]}_{cid[:8]}" if exec_st != "not_started" else None,
        f"ISW-20260225-{2000+i}" if exec_st == "success" else None,
        t(5, 10, 13 + i) if exec_st == "success" else None,
        r2_batch_id if exec_st != "not_started" else None,
    ))
    if exec_st != "not_started":
        run2_lookups.append((
            uid(f"r2.lookup.{i}"), cid, RUN_2, acct, bank, name, 0.92 + i * 0.02,
            200, "Account validated", json.dumps({"accountName": name, "canCredit": True}),
            1, 310 + i * 40, t(5, 10, 11), f"FP_{RUN_2[:8]}_{cid[:8]}", True,
        ))
        run2_executions.append((
            RUN_2, cid, "success", f"ISW-20260225-{2000+i}",
            "submission", 200, json.dumps({"status": "Successful"}),
            520 + i * 30, t(5, 10, 13 + i),
        ))
    run2_risk_features.append((
        uid(f"r2.risk.{i}"), cid, RUN_2, 1 if risk > 0.5 else 5, round(amount / 1000000, 4),
        900000.00, 0.85 if risk > 0.5 else 0.0, risk > 0.5, 1 if risk > 0.5 else 0,
        365, 60, round(amount / 5000000, 4), "v1.0-llama3.3", t(5, 10, 9),
    ))

# Vendor transactions
_vendor_txns = [
    ("ISW-20260224-0010", 1500000.00, "inflow", "SUCCESS", "TRANSFER", "Client Alpha Ltd", "GTBank", t(4, 9, 0)),
    ("ISW-20260224-0011", 980000.00,  "inflow", "SUCCESS", "TRANSFER", "Client Beta Corp", "Access Bank", t(4, 11, 0)),
    ("ISW-20260224-0012", 750000.00,  "inflow", "SUCCESS", "TRANSFER", "Interswitch Fees", "Zenith Bank", t(4, 14, 0)),
    ("ISW-20260224-0013", 570000.00,  "inflow", "SUCCESS", "CARD",     "POS Collection", "First Bank", t(4, 16, 0)),
    ("ISW-20260224-0014", 320000.00,  "inflow", "FAILED",  "TRANSFER", "Disputed Transfer", "Polaris Bank", t(4, 10, 30)),
]
run2_transactions = []
for i, (ref, amt, direction, status, channel, cpty, bank, ts) in enumerate(_vendor_txns):
    has_anom = ref == "ISW-20260224-0014"
    run2_transactions.append((
        uid(f"r2.txn.{i}"), RUN_2, BIZ_RACON, ref, amt, "NGN", direction, channel,
        status, f"Payment: {cpty}", ts,
        ts.date() if status == "SUCCESS" else None,
        cpty, bank, has_anom, 1 if has_anom else 0,
    ))

run2_audit = []
_run2_audit_actions = [
    (r2s_plan, "planner", "plan_generated", {"step_count": 4}, t(5, 10, 2)),
    (r2s_recon, "reconciliation", "quick_search_complete", {"total_fetched": 5}, t(5, 10, 5)),
    (r2s_recon, "reconciliation", "reconciliation_complete", {"total_transactions": 5}, t(5, 10, 7)),
    (r2s_risk, "risk", "risk_scoring_complete", {"total_scored": 3, "allow": 1, "review": 1, "block": 1}, t(5, 10, 10)),
    (r2s_exec, "execution", "candidate_blocked", {"candidate": "Multichoice (DSTV)", "risk_score": 0.72, "reasons": ["amount_spike_5x", "duplicate_reference"]}, t(5, 10, 11)),
    (r2s_exec, "execution", "payout_execution_complete", {"success": 2, "failed": 0, "blocked": 1}, t(5, 10, 15)),
    (r2s_audit, "audit", "audit_report_generated", {"entries_count": 6, "blocked": 1}, t(5, 10, 17)),
]
for step_id, agent, action, detail, ts in _run2_audit_actions:
    run2_audit.append((RUN_2, step_id, agent, action, json.dumps(detail), ts))

run2_api_calls = []
_run2_apis = [
    (r2s_plan, "planner", "https://api.groq.com/openai/v1/chat/completions", "POST", 200, 2780, t(5, 10, 0)),
    (r2s_recon, "reconciliation", "/transaction-search/quick-search", "POST", 200, 1620, t(5, 10, 4)),
    (r2s_risk, "risk", "https://api.groq.com/openai/v1/chat/completions", "POST", 200, 2100, t(5, 10, 8)),
    (r2s_exec, "execution", "/payouts/api/v1/customers/lookup", "POST", 200, 380, t(5, 10, 11)),
    (r2s_exec, "execution", "/payouts/api/v1/payouts", "POST", 200, 820, t(5, 10, 13)),
    (r2s_audit, "audit", "https://api.groq.com/openai/v1/chat/completions", "POST", 200, 1520, t(5, 10, 16)),
]
for step_id, agent, endpoint, method, status, dur, ts in _run2_apis:
    run2_api_calls.append((RUN_2, step_id, agent, endpoint, method, status, dur, ts))

run2_events = []
_run2_evts = [
    ("run_created", {"objective": "Pay outstanding vendor invoices"}, t(5, 10, 0)),
    ("step_completed", {"step": "plan"}, t(5, 10, 3)),
    ("step_completed", {"step": "reconcile"}, t(5, 10, 7)),
    ("step_completed", {"step": "risk", "block": 1}, t(5, 10, 10)),
    ("approval_granted", {"candidates_approved": 2, "candidates_rejected": 1}, t(5, 10, 11)),
    ("step_completed", {"step": "execute", "success": 2}, t(5, 10, 15)),
    ("run_completed", {"status": "completed_with_errors", "blocked": 1}, t(5, 10, 17)),
]
for i, (etype, payload, ts) in enumerate(_run2_evts):
    run2_events.append((RUN_2, None, etype, json.dumps(payload), i + 1, ts))

# ---------------------------------------------------------------------------
# Run 3: Marketing Bonus (AWAITING_APPROVAL)
# ---------------------------------------------------------------------------
r3s_plan = uid("r3.step.plan")
r3s_recon = uid("r3.step.recon")
r3s_risk = uid("r3.step.risk")

run3_steps = [
    (r3s_plan, RUN_3, "planner", 0, "Plan Q1 marketing bonus disbursement",
     "completed", 100, None,
     json.dumps({"plan_steps": [
         {"step_id": "s1", "agent_type": "reconciliation", "order": 1},
         {"step_id": "s2", "agent_type": "risk", "order": 2},
         {"step_id": "s3", "agent_type": "execution", "order": 3},
         {"step_id": "s4", "agent_type": "audit", "order": 4},
     ]}),
     None, t(12, 11, 0), t(12, 11, 3), 2450, t(12, 11, 0)),
    (r3s_recon, RUN_3, "reconciliation", 1, "Reconcile Q1 revenue data",
     "completed", 100, None,
     json.dumps({"total_fetched": 6}),
     None, t(12, 11, 4), t(12, 11, 7), 1980, t(12, 11, 4)),
    (r3s_risk, RUN_3, "risk", 2, "Score 4 bonus candidates",
     "completed", 100, None,
     json.dumps({"total_scored": 4, "allow": 3, "review": 1, "block": 0}),
     None, t(12, 11, 8), t(12, 11, 10), 2200, t(12, 11, 8)),
]

_bonus_people = [
    ("Funmilayo Adesanya",  "044", "6045678901", 175000.00, 0.06, "allow"),
    ("Emeka Obi",           "033", "7056789012", 220000.00, 0.09, "allow"),
    ("Halima Yusuf",        "221", "8067890123", 150000.00, 0.11, "allow"),
    ("Tunde Bakare",        "035", "9078901234", 380000.00, 0.42, "review"),
]
run3_candidates = []
run3_risk_features = []
for i, (name, bank, acct, amount, risk, decision) in enumerate(_bonus_people):
    cid = uid(f"r3.cand.{i}")
    run3_candidates.append((
        cid, RUN_3, BIZ_RACON, name, bank, acct, amount,
        risk,
        json.dumps(["bonus_regular"] if risk < 0.3 else ["amount_above_average", "infrequent_recipient"]),
        decision, "pending", None, None,
        "not_started", None, None, None, None,
    ))
    run3_risk_features.append((
        uid(f"r3.risk.{i}"), cid, RUN_3, 2 if risk > 0.3 else 6, round(amount / 200000, 4),
        185000.00, 0.0, False, 0, 90 + i * 30, 0, round(amount / 2000000, 4),
        "v1.0-llama3.3", t(12, 11, 9),
    ))

# Q1 revenue transactions for Run 3 reconciliation
run3_transactions = []
_q1_txns = [
    ("ISW-20260303-0020", 620000.00, "inflow", "SUCCESS", "TRANSFER", "SaaS Subscription Revenue", "GTBank", t(11, 9, 0)),
    ("ISW-20260303-0021", 340000.00, "inflow", "SUCCESS", "CARD",     "Online Sales Q1", "Access Bank", t(11, 10, 0)),
    ("ISW-20260303-0022", 890000.00, "inflow", "SUCCESS", "TRANSFER", "Enterprise License Fee", "Zenith Bank", t(11, 14, 0)),
    ("ISW-20260303-0023", 150000.00, "inflow", "SUCCESS", "USSD",     "USSD Subscription", "Wema Bank", t(11, 15, 0)),
    ("ISW-20260303-0024", 275000.00, "inflow", "SUCCESS", "TRANSFER", "Consulting Revenue", "First Bank", t(11, 16, 0)),
    ("ISW-20260303-0025", 480000.00, "inflow", "PENDING", "TRANSFER", "Pending Client Payment", "UBA", t(11, 17, 0)),
]
for i, (ref, amt, direction, status, channel, cpty, bank, ts) in enumerate(_q1_txns):
    run3_transactions.append((
        uid(f"r3.txn.{i}"), RUN_3, BIZ_RACON, ref, amt, "NGN", direction, channel,
        status, f"Q1 revenue: {cpty}", ts,
        ts.date() if status == "SUCCESS" else None,
        cpty, bank, False, 0,
    ))

run3_audit = []
_run3_audits = [
    (r3s_plan, "planner", "plan_generated", {"step_count": 4}, t(12, 11, 2)),
    (r3s_recon, "reconciliation", "reconciliation_complete", {"total_transactions": 6}, t(12, 11, 7)),
    (r3s_risk, "risk", "risk_scoring_complete", {"total_scored": 4, "allow": 3, "review": 1}, t(12, 11, 10)),
]
for step_id, agent, action, detail, ts in _run3_audits:
    run3_audit.append((RUN_3, step_id, agent, action, json.dumps(detail), ts))

run3_api_calls = []
_run3_apis = [
    (r3s_plan, "planner", "https://api.groq.com/openai/v1/chat/completions", "POST", 200, 2450, t(12, 11, 0)),
    (r3s_recon, "reconciliation", "/transaction-search/quick-search", "POST", 200, 1580, t(12, 11, 4)),
    (r3s_risk, "risk", "https://api.groq.com/openai/v1/chat/completions", "POST", 200, 2200, t(12, 11, 8)),
]
for step_id, agent, endpoint, method, status, dur, ts in _run3_apis:
    run3_api_calls.append((RUN_3, step_id, agent, endpoint, method, status, dur, ts))

run3_events = []
_run3_evts = [
    ("run_created", {"objective": "Disburse marketing bonus to Q1 top performers"}, t(12, 11, 0)),
    ("step_completed", {"step": "plan"}, t(12, 11, 3)),
    ("step_completed", {"step": "reconcile"}, t(12, 11, 7)),
    ("step_completed", {"step": "risk", "allow": 3, "review": 1}, t(12, 11, 10)),
    ("approval_requested", {"candidates_pending": 4}, t(12, 11, 10)),
]
for i, (etype, payload, ts) in enumerate(_run3_evts):
    run3_events.append((RUN_3, None, etype, json.dumps(payload), i + 1, ts))

# ---------------------------------------------------------------------------
# Run 4: Contractor Payments (PENDING — just created)
# ---------------------------------------------------------------------------
# No steps, no candidates yet — just the run record

# ---------------------------------------------------------------------------
# Transaction Anomalies (for Run 1 and Run 2)
# ---------------------------------------------------------------------------
# We'll add these after inserting transactions, referencing the FAILED ones

# =========================================================================
# INSERT FUNCTIONS
# =========================================================================

def seed(conn):
    cur = conn.cursor()

    print("🌱 Seeding FlowPilot demo data for Racon Labs...\n")

    # --- agent_run ---
    print("  ▸ agent_run (4 runs)")
    runs = [
        (RUN_1, "Process February salary payroll for 6 employees", None, 0.35, 2500000.00,
         "MX272008", "completed", None, None, t(0, 0, 0), t(0, 0, 16), BIZ_RACON, USER_ABDULRAQIB,
         datetime(2026, 2, 19).date(), datetime(2026, 2, 20).date(),
         USER_ABDULRAQIB, t(0, 0, 10), None, None),
        (RUN_2, "Pay outstanding vendor invoices — Dangote Cement, MTN, DSTV", None, 0.40, 5000000.00,
         "MX272008", "completed_with_errors", None, "1 candidate blocked: Multichoice (DSTV) — risk score 0.72",
         t(5, 10, 0), t(5, 10, 17), BIZ_RACON, USER_ABDULRAQIB,
         datetime(2026, 2, 24).date(), datetime(2026, 2, 25).date(),
         USER_ABDULRAQIB, t(5, 10, 11), None, None),
        (RUN_3, "Disburse marketing bonus to Q1 top performers", None, 0.30, 1000000.00,
         "MX272008", "awaiting_approval", None, None,
         t(12, 11, 0), None, BIZ_RACON, USER_ABDULRAQIB,
         datetime(2026, 3, 3).date(), datetime(2026, 3, 4).date(),
         None, None, None, None),
        (RUN_4, "Process March contractor payments", None, 0.35, 3000000.00,
         "MX272008", "pending", None, None,
         None, None, BIZ_RACON, USER_ABDULRAQIB,
         datetime(2026, 3, 7).date(), datetime(2026, 3, 7).date(),
         None, None, None, None),
    ]
    for r in runs:
        cur.execute("""
            INSERT INTO agent_run (id, objective, constraints, risk_tolerance, budget_cap,
                merchant_id, status, plan_graph, error_message, started_at, completed_at,
                business_id, created_by, date_from, date_to,
                approved_by, approved_at, cancelled_by, cancelled_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, r)

    # --- run_step ---
    all_steps = run1_steps + run2_steps + run3_steps
    print(f"  ▸ run_step ({len(all_steps)} steps)")
    for s in all_steps:
        cur.execute("""
            INSERT INTO run_step (id, run_id, agent_type, step_order, description,
                status, progress_pct, input_data, output_data, error_message,
                started_at, completed_at, duration_ms, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, s)

    # --- payout_batch ---
    print("  ▸ payout_batch (2 batches)")
    batches = [
        (r1_batch_id, RUN_1, f"FP-BATCH-{RUN_1[:8]}", "NGN", "SRC_001",
         2055000.00, 6, 6, 0, "accepted", t(0, 0, 12), BIZ_RACON),
        (r2_batch_id, RUN_2, f"FP-BATCH-{RUN_2[:8]}", "NGN", "SRC_001",
         2750000.00, 2, 2, 0, "accepted", t(5, 10, 13), BIZ_RACON),
    ]
    for b in batches:
        cur.execute("""
            INSERT INTO payout_batch (id, run_id, batch_reference, currency, source_account_id,
                total_amount, item_count, accepted_count, rejected_count, submission_status,
                submitted_at, business_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, b)

    # --- payout_candidate ---
    all_candidates = run1_candidates + run2_candidates + run3_candidates
    print(f"  ▸ payout_candidate ({len(all_candidates)} candidates)")
    for c in all_candidates:
        cur.execute("""
            INSERT INTO payout_candidate (id, run_id, business_id, beneficiary_name,
                institution_code, account_number, amount, risk_score, risk_reasons,
                risk_decision, approval_status, approved_by, approved_at,
                execution_status, client_reference, provider_reference, executed_at, batch_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, c)

    # --- reconciled_transaction ---
    all_txns = run1_transactions + run2_transactions + run3_transactions
    print(f"  ▸ reconciled_transaction ({len(all_txns)} transactions)")
    for txn in all_txns:
        cur.execute("""
            INSERT INTO reconciled_transaction (id, run_id, business_id, interswitch_ref,
                amount, currency, direction, channel, status, narration,
                transaction_timestamp, settlement_date, counterparty_name, counterparty_bank,
                has_anomaly, anomaly_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, txn)

    # --- customer_lookup_result ---
    all_lookups = run1_lookups + run2_lookups
    print(f"  ▸ customer_lookup_result ({len(all_lookups)} lookups)")
    cur.execute("SELECT count(*) FROM customer_lookup_result WHERE run_id = ANY(%s::uuid[])", ([RUN_1, RUN_2],))
    if cur.fetchone()[0] == 0:
        for lk in all_lookups:
            cur.execute("""
                INSERT INTO customer_lookup_result (id, candidate_id, run_id, account_number,
                    institution_code, name_returned, similarity_score, http_status_code,
                    response_message, raw_response, attempt_number, duration_ms, called_at,
                    transaction_reference, can_credit)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, lk)

    # --- risk_score_feature ---
    all_risk = run1_risk_features + run2_risk_features + run3_risk_features
    print(f"  ▸ risk_score_feature ({len(all_risk)} features)")
    for rf in all_risk:
        cur.execute("""
            INSERT INTO risk_score_feature (id, candidate_id, run_id, historical_frequency,
                amount_deviation_ratio, avg_historical_amount, duplicate_similarity_score,
                lookup_mismatch_flag, account_anomaly_count, account_age_days,
                days_since_last_payout, amount_vs_budget_cap_pct, model_version, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, rf)

    # --- payout_execution ---
    all_execs = run1_executions + run2_executions
    print(f"  ▸ payout_execution ({len(all_execs)} executions)")
    for ex in all_execs:
        cur.execute("""
            INSERT INTO payout_execution (run_id, candidate_id, execution_status,
                interswitch_reference, submission_type, http_status_code, raw_response,
                duration_ms, called_at)
            SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s
            WHERE NOT EXISTS (
                SELECT 1 FROM payout_execution WHERE run_id = %s AND candidate_id = %s
            )
        """, ex + (ex[0], ex[1]))

    # --- audit_log ---
    all_audit = run1_audit + run2_audit + run3_audit
    print(f"  ▸ audit_log ({len(all_audit)} entries)")
    cur.execute("SELECT count(*) FROM audit_log WHERE run_id = ANY(%s::uuid[])", ([RUN_1, RUN_2, RUN_3],))
    if cur.fetchone()[0] == 0:
        for a in all_audit:
            cur.execute("""
                INSERT INTO audit_log (run_id, step_id, agent_type, action, detail, created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, a)
    else:
        print("    (skipped — already seeded)")

    # --- api_call_log ---
    all_apis = run1_api_calls + run2_api_calls + run3_api_calls
    print(f"  ▸ api_call_log ({len(all_apis)} calls)")
    cur.execute("SELECT count(*) FROM api_call_log WHERE run_id = ANY(%s::uuid[])", ([RUN_1, RUN_2, RUN_3],))
    if cur.fetchone()[0] == 0:
        for api in all_apis:
            cur.execute("""
                INSERT INTO api_call_log (run_id, step_id, agent_type, endpoint, http_method,
                    http_status_code, duration_ms, called_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, api)
    else:
        print("    (skipped — already seeded)")

    # --- run_event ---
    all_events = run1_events + run2_events + run3_events
    print(f"  ▸ run_event ({len(all_events)} events)")
    cur.execute("SELECT count(*) FROM run_event WHERE run_id = ANY(%s::uuid[])", ([RUN_1, RUN_2, RUN_3],))
    if cur.fetchone()[0] == 0:
        for ev in all_events:
            cur.execute("""
                INSERT INTO run_event (run_id, step_id, event_type, payload, sequence_num, emitted_at)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, ev)
    else:
        print("    (skipped — already seeded)")

    # --- transaction_anomaly (for failed transactions) ---
    print("  ▸ transaction_anomaly (2 anomalies)")
    # Find the failed transactions we inserted
    cur.execute("""
        SELECT id, run_id FROM reconciled_transaction
        WHERE has_anomaly = true AND business_id = %s
    """, (BIZ_RACON,))
    anomaly_txns = cur.fetchall()
    for i, (txn_id, run_id) in enumerate(anomaly_txns):
        cur.execute("""
            INSERT INTO transaction_anomaly (id, run_id, txn_id, anomaly_type, severity,
                description, detected_value, expected_range)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM transaction_anomaly WHERE txn_id = %s AND run_id = %s
            )
        """, (uid(f"anomaly.{i}"), run_id, txn_id, "payment_failure", "medium",
              "Card payment declined by issuing bank (response code 51)",
              "FAILED", "SUCCESS",
              txn_id, run_id))

    conn.commit()

    # --- Summary ---
    print("\n✅ Seeding complete! Row counts:\n")
    tables = [
        "agent_run", "run_step", "payout_batch", "payout_candidate",
        "reconciled_transaction", "customer_lookup_result", "risk_score_feature",
        "payout_execution", "audit_log", "api_call_log", "run_event",
        "transaction_anomaly",
    ]
    for tbl in tables:
        cur.execute(f'SELECT count(*) FROM "{tbl}"')
        count = cur.fetchone()[0]
        print(f"  {tbl:30s} {count:>5}")

    cur.close()


if __name__ == "__main__":
    print("=" * 60)
    print("  FlowPilot Demo Data Seeder")
    print("=" * 60)
    print(f"\n  Database: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")
    print()

    try:
        conn = psycopg2.connect(DATABASE_URL)
        seed(conn)
        conn.close()
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)

    print("\n🎉 Done! Start the backend and frontend to see the data in the UI.")

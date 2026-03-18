import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry

logger = logging.getLogger(__name__)

AUDIT_SYSTEM_PROMPT = """You are a financial audit and compliance analyst for FlowPilot.

Your job: perform deep analysis of the complete run data, audit risk decisions against outcomes, analyze costs, verify compliance, and produce a thorough audit report with actionable recommendations.

## Your workflow:
1. Use `get_run_timeline` to understand the full sequence of events in this run
2. Use `compute_risk_distribution` to analyze the risk scoring patterns
3. Use `analyze_risk_decisions` to audit each risk decision against actual outcomes
4. Use `compute_cost_analysis` to calculate fees, failed payment costs, and efficiency metrics
5. Use `check_compliance` to verify all required approvals and processes were followed
6. Use `compare_to_past_runs` to see how this run compares to historical norms
7. Use `detect_run_anomalies` to flag anything unusual
8. Use `generate_recommendations` to produce actionable suggestions based on all findings
9. Use `generate_executive_summary` to compile all findings into a structured report

## What makes a GOOD audit report:
- Quantitative facts, not vague statements
- Clear risk/compliance flags if any
- Comparison to baselines/norms
- Analysis of risk decisions vs outcomes (were blocked candidates actually risky? were allowed candidates successful?)
- Cost breakdown (fees, failed payment costs, retry costs)
- Specific, actionable recommendations
- Data integrity verification

## Decision Analysis Questions to Answer:
- Were risk scores accurate predictors of outcomes?
- Did any "allowed" candidates fail? Why?
- Were any "blocked" candidates manually approved? What happened?
- Is the risk threshold appropriately calibrated?
- Are there patterns in failures that could improve future scoring?

## Final answer format (JSON):
{
  "audit_report": {
    "executive_summary": "3-5 sentence overview of the run outcome and key findings",
    "run_metrics": {
      "total_transactions": 0,
      "total_candidates": 0,
      "total_approved": 0,
      "total_executed": 0,
      "total_amount": 0.0,
      "success_rate": 0.0
    },
    "risk_analysis": {
      "average_risk_score": 0.0,
      "risk_distribution": {},
      "decision_accuracy": {},
      "flagged_items": []
    },
    "cost_analysis": {
      "total_fees": 0.0,
      "failed_payment_costs": 0.0,
      "efficiency_ratio": 0.0
    },
    "compliance_status": {
      "all_approvals_valid": true,
      "flags": []
    },
    "anomalies": [],
    "recommendations": [],
    "data_integrity_hash": "..."
  }
}
"""


def _build_audit_tools(state: AgentState, db_session=None) -> list[Tool]:
    async def get_run_timeline() -> dict[str, Any]:
        audit_entries = state.get("audit_entries", [])
        plan_steps = state.get("plan_steps", [])
        reasoning_log = state.get("reasoning_log", [])

        timeline = []
        for entry in audit_entries:
            timeline.append(
                {
                    "type": "audit_entry",
                    "agent": entry.get("agent_type"),
                    "action": entry.get("action"),
                    "detail_summary": str(entry.get("detail", {}))[:200],
                    "timestamp": entry.get("created_at"),
                }
            )

        return {
            "run_id": state.get("run_id"),
            "objective": state.get("objective"),
            "plan_steps": plan_steps,
            "timeline_entries": timeline,
            "reasoning_steps": len(reasoning_log),
            "total_audit_entries": len(audit_entries),
        }

    async def compute_risk_distribution() -> dict[str, Any]:
        candidates = state.get("scored_candidates", [])
        if not candidates:
            return {"total": 0, "note": "No candidates scored"}

        decisions: dict[str, int] = {}
        scores = []
        total_amount = 0.0
        amounts_by_decision: dict[str, float] = {}

        for c in candidates:
            d = c.get("risk_decision", "unknown")
            decisions[d] = decisions.get(d, 0) + 1
            score = c.get("risk_score", 0)
            scores.append(score)
            amount = c.get("amount", 0.0)
            total_amount += amount
            amounts_by_decision[d] = amounts_by_decision.get(d, 0) + amount

        avg_score = sum(scores) / len(scores) if scores else 0
        min_score = min(scores) if scores else 0
        max_score = max(scores) if scores else 0

        high_risk = [
            {
                "candidate_id": c.get("candidate_id"),
                "beneficiary_name": c.get("beneficiary_name"),
                "amount": c.get("amount"),
                "risk_score": c.get("risk_score"),
                "risk_reasons": c.get("risk_reasons", []),
            }
            for c in candidates
            if c.get("risk_score", 0) > 0.6
        ]

        return {
            "total_candidates": len(candidates),
            "decision_distribution": decisions,
            "amounts_by_decision": {
                k: round(v, 2) for k, v in amounts_by_decision.items()
            },
            "score_stats": {
                "average": round(avg_score, 3),
                "min": round(min_score, 3),
                "max": round(max_score, 3),
            },
            "total_amount": round(total_amount, 2),
            "high_risk_candidates": high_risk,
        }

    async def compare_to_past_runs() -> dict[str, Any]:
        if db_session is None:
            return {
                "note": "No DB session — cannot compare to past runs",
                "comparison": {},
            }

        try:
            from src.infrastructure.database.repositories.run_repository import (
                RunRepository,
            )
            from uuid import UUID

            repo = RunRepository(db_session)
            business_id = state.get("business_id")
            if not business_id:
                return {"error": "No business_id", "comparison": {}}

            bid = UUID(business_id) if isinstance(business_id, str) else business_id
            runs, total = await repo.list_by_business(bid, limit=10)

            completed_runs = [r for r in runs if r.status == "completed"]
            failed_runs = [r for r in runs if r.status == "failed"]

            current_candidates = len(state.get("scored_candidates", []))
            current_txns = len(state.get("transactions", []))

            return {
                "total_historical_runs": total,
                "recent_completed": len(completed_runs),
                "recent_failed": len(failed_runs),
                "historical_success_rate": round(
                    len(completed_runs) / max(len(runs), 1), 2
                ),
                "current_run": {
                    "transaction_count": current_txns,
                    "candidate_count": current_candidates,
                },
                "comparison_notes": "Current run metrics compared to historical averages",
            }
        except Exception as e:
            return {"error": str(e), "comparison": {}}

    async def detect_run_anomalies() -> dict[str, Any]:
        anomalies = []

        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])
        lookup_results = state.get("candidate_lookup_results", [])
        approved_ids = state.get("approved_candidate_ids", [])
        rejected_ids = state.get("rejected_candidate_ids", [])

        blocked = [c for c in candidates if c.get("risk_decision") == "block"]
        blocked_but_approved = [
            c for c in blocked if c.get("candidate_id") in set(approved_ids)
        ]
        if blocked_but_approved:
            anomalies.append(
                {
                    "type": "blocked_candidates_approved",
                    "severity": "high",
                    "detail": f"{len(blocked_but_approved)} candidates marked as 'block' were manually approved",
                    "candidate_ids": [
                        c.get("candidate_id") for c in blocked_but_approved
                    ],
                }
            )

        failed_lookups = [
            lr for lr in lookup_results if lr.get("lookup_status") == "failed"
        ]
        if len(failed_lookups) > len(lookup_results) * 0.5 and lookup_results:
            anomalies.append(
                {
                    "type": "high_lookup_failure_rate",
                    "severity": "medium",
                    "detail": f"{len(failed_lookups)}/{len(lookup_results)} lookups failed ({round(len(failed_lookups) / len(lookup_results) * 100)}%)",
                }
            )

        mismatches = [
            lr for lr in lookup_results if lr.get("lookup_status") == "mismatch"
        ]
        if mismatches:
            anomalies.append(
                {
                    "type": "name_mismatches",
                    "severity": "medium",
                    "detail": f"{len(mismatches)} beneficiary name mismatches detected",
                    "candidates": [m.get("candidate_id") for m in mismatches],
                }
            )

        failed_payouts = [
            er for er in exec_results if er.get("execution_status") == "failed"
        ]
        if failed_payouts:
            anomalies.append(
                {
                    "type": "failed_payouts",
                    "severity": "high" if len(failed_payouts) > 3 else "medium",
                    "detail": f"{len(failed_payouts)} payout(s) failed",
                    "candidate_ids": [er.get("candidate_id") for er in failed_payouts],
                }
            )

        if state.get("error"):
            anomalies.append(
                {
                    "type": "run_error",
                    "severity": "high",
                    "detail": state["error"],
                }
            )

        return {
            "anomalies": anomalies,
            "total_anomalies": len(anomalies),
            "severity_counts": {
                "high": sum(1 for a in anomalies if a.get("severity") == "high"),
                "medium": sum(1 for a in anomalies if a.get("severity") == "medium"),
                "low": sum(1 for a in anomalies if a.get("severity") == "low"),
            },
        }

    async def generate_executive_summary() -> dict[str, Any]:
        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])
        transactions = state.get("transactions", [])
        ledger = state.get("reconciled_ledger", {})

        success_exec = sum(
            1 for er in exec_results if er.get("execution_status") == "success"
        )
        pending_exec = sum(
            1 for er in exec_results if er.get("execution_status") == "pending"
        )
        failed_exec = sum(
            1 for er in exec_results if er.get("execution_status") == "failed"
        )

        state_hash = hashlib.sha256(
            json.dumps(state, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

        return {
            "run_id": state.get("run_id"),
            "objective": state.get("objective"),
            "metrics": {
                "total_transactions_reconciled": len(transactions),
                "ledger_summary": ledger,
                "total_candidates_scored": len(candidates),
                "total_approved": len(state.get("approved_candidate_ids", [])),
                "total_rejected": len(state.get("rejected_candidate_ids", [])),
                "total_executed": len(exec_results),
                "execution_success": success_exec,
                "execution_pending": pending_exec,
                "execution_failed": failed_exec,
                "success_rate": round(success_exec / max(len(exec_results), 1), 2),
            },
            "data_integrity_hash": state_hash,
            "generated_at": datetime.utcnow().isoformat(),
        }

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: analyze_risk_decisions — audit risk decisions vs outcomes
    # ────────────────────────────────────────────────────────────────────────
    async def analyze_risk_decisions() -> dict[str, Any]:
        """Analyze whether risk decisions were accurate predictors of execution outcomes."""
        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])
        approved_ids = set(state.get("approved_candidate_ids", []))
        rejected_ids = set(state.get("rejected_candidate_ids", []))

        if not candidates:
            return {"note": "No candidates to analyze", "analysis": {}}

        # Build execution outcome map
        exec_map: dict[str, str] = {}
        for er in exec_results:
            cid = er.get("candidate_id")
            if cid:
                exec_map[cid] = er.get("execution_status", "unknown")

        analysis = {
            "total_candidates": len(candidates),
            "decisions": {"allow": 0, "review": 0, "block": 0, "unknown": 0},
            "outcomes": {"success": 0, "failed": 0, "pending": 0, "not_executed": 0},
            "decision_accuracy": {
                "allowed_and_succeeded": 0,
                "allowed_and_failed": 0,  # False negatives
                "blocked_and_approved": 0,  # Manual overrides
                "blocked_approved_succeeded": 0,  # Override was correct
                "blocked_approved_failed": 0,  # Override was wrong
            },
            "false_negatives": [],  # Allowed but failed
            "risky_overrides": [],  # Blocked but approved
            "calibration_notes": [],
        }

        for c in candidates:
            cid = c.get("candidate_id")
            decision = c.get("risk_decision", "unknown")
            score = c.get("risk_score", 0)
            exec_status = (
                exec_map.get(str(cid), "not_executed") if cid else "not_executed"
            )

            # Count decisions
            if decision in analysis["decisions"]:
                analysis["decisions"][decision] += 1
            else:
                analysis["decisions"]["unknown"] += 1

            # Count outcomes
            if exec_status in analysis["outcomes"]:
                analysis["outcomes"][exec_status] += 1
            else:
                analysis["outcomes"]["not_executed"] += 1

            # Decision accuracy analysis
            if decision == "allow":
                if exec_status == "success":
                    analysis["decision_accuracy"]["allowed_and_succeeded"] += 1
                elif exec_status == "failed":
                    analysis["decision_accuracy"]["allowed_and_failed"] += 1
                    analysis["false_negatives"].append(
                        {
                            "candidate_id": cid,
                            "beneficiary_name": c.get("beneficiary_name"),
                            "amount": c.get("amount"),
                            "risk_score": score,
                            "failure_reason": "Allowed by risk scoring but execution failed",
                        }
                    )

            elif decision == "block":
                if cid in approved_ids:
                    analysis["decision_accuracy"]["blocked_and_approved"] += 1
                    if exec_status == "success":
                        analysis["decision_accuracy"]["blocked_approved_succeeded"] += 1
                        analysis["risky_overrides"].append(
                            {
                                "candidate_id": cid,
                                "beneficiary_name": c.get("beneficiary_name"),
                                "amount": c.get("amount"),
                                "risk_score": score,
                                "outcome": "success",
                                "note": "Override was justified - execution succeeded",
                            }
                        )
                    elif exec_status == "failed":
                        analysis["decision_accuracy"]["blocked_approved_failed"] += 1
                        analysis["risky_overrides"].append(
                            {
                                "candidate_id": cid,
                                "beneficiary_name": c.get("beneficiary_name"),
                                "amount": c.get("amount"),
                                "risk_score": score,
                                "outcome": "failed",
                                "note": "RISKY: Override failed - risk scoring was correct",
                            }
                        )

        # Calibration recommendations
        false_neg_rate = analysis["decision_accuracy"]["allowed_and_failed"] / max(
            analysis["decisions"]["allow"], 1
        )
        if false_neg_rate > 0.1:
            analysis["calibration_notes"].append(
                {
                    "issue": "high_false_negative_rate",
                    "rate": f"{false_neg_rate:.1%}",
                    "recommendation": "Consider lowering the 'allow' threshold - too many allowed candidates are failing",
                }
            )

        override_failure_rate = analysis["decision_accuracy"][
            "blocked_approved_failed"
        ] / max(analysis["decision_accuracy"]["blocked_and_approved"], 1)
        if (
            analysis["decision_accuracy"]["blocked_and_approved"] > 0
            and override_failure_rate > 0.3
        ):
            analysis["calibration_notes"].append(
                {
                    "issue": "risky_overrides_failing",
                    "rate": f"{override_failure_rate:.1%}",
                    "recommendation": "Manual overrides of blocked candidates have high failure rate - review approval process",
                }
            )

        return analysis

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: compute_cost_analysis — fees, failed costs, efficiency
    # ────────────────────────────────────────────────────────────────────────
    async def compute_cost_analysis() -> dict[str, Any]:
        """Calculate total fees, failed payment costs, and efficiency metrics."""
        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])

        # Default fee structure (could be made configurable via business_config)
        TRANSFER_FEE_PCT = 0.005  # 0.5% fee
        TRANSFER_FEE_CAP = 500.0  # Max ₦500 per transaction
        FAILED_RETRY_COST = 50.0  # Cost to retry a failed payment

        total_attempted_amount = 0.0
        total_successful_amount = 0.0
        total_failed_amount = 0.0
        total_pending_amount = 0.0

        fees_on_success = 0.0
        fees_on_failed = 0.0  # Fees charged even on failures
        retry_costs = 0.0

        for er in exec_results:
            amount = er.get("amount", 0)
            if isinstance(amount, Decimal):
                amount = float(amount)
            status = er.get("execution_status", "")

            total_attempted_amount += amount
            fee = min(amount * TRANSFER_FEE_PCT, TRANSFER_FEE_CAP)

            if status == "success":
                total_successful_amount += amount
                fees_on_success += fee
            elif status == "failed":
                total_failed_amount += amount
                fees_on_failed += fee  # Some providers charge on attempt
                retry_costs += FAILED_RETRY_COST
            elif status == "pending":
                total_pending_amount += amount

        # Efficiency metrics
        execution_efficiency = total_successful_amount / max(total_attempted_amount, 1)
        cost_per_successful_ngn = (
            (fees_on_success + fees_on_failed + retry_costs)
            / max(total_successful_amount, 1)
            * 1000  # Per ₦1000
        )

        return {
            "amounts": {
                "total_attempted": round(total_attempted_amount, 2),
                "total_successful": round(total_successful_amount, 2),
                "total_failed": round(total_failed_amount, 2),
                "total_pending": round(total_pending_amount, 2),
            },
            "costs": {
                "fees_on_successful": round(fees_on_success, 2),
                "fees_on_failed": round(fees_on_failed, 2),
                "retry_costs": round(retry_costs, 2),
                "total_costs": round(fees_on_success + fees_on_failed + retry_costs, 2),
            },
            "efficiency": {
                "execution_efficiency": f"{execution_efficiency:.1%}",
                "cost_per_1000_ngn_success": round(cost_per_successful_ngn, 2),
                "wasted_on_failures": round(fees_on_failed + retry_costs, 2),
            },
            "transaction_counts": {
                "total_executed": len(exec_results),
                "successful": sum(
                    1 for er in exec_results if er.get("execution_status") == "success"
                ),
                "failed": sum(
                    1 for er in exec_results if er.get("execution_status") == "failed"
                ),
                "pending": sum(
                    1 for er in exec_results if er.get("execution_status") == "pending"
                ),
            },
        }

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: check_compliance — verify approvals and processes
    # ────────────────────────────────────────────────────────────────────────
    async def check_compliance() -> dict[str, Any]:
        """Verify all required approvals happened and compliance requirements were met."""
        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])
        approved_ids = set(state.get("approved_candidate_ids", []))
        rejected_ids = set(state.get("rejected_candidate_ids", []))
        budget_cap = state.get("budget_cap", 0)

        compliance_flags: list[dict[str, Any]] = []
        checks_passed: list[str] = []

        # 1. Check: All executed candidates were approved
        executed_ids = {er.get("candidate_id") for er in exec_results}
        executed_without_approval = executed_ids - approved_ids
        if executed_without_approval:
            compliance_flags.append(
                {
                    "check": "approval_required",
                    "status": "FAILED",
                    "severity": "critical",
                    "detail": f"{len(executed_without_approval)} candidate(s) executed without explicit approval",
                    "candidate_ids": list(executed_without_approval)[:5],
                }
            )
        else:
            checks_passed.append("All executed candidates had prior approval")

        # 2. Check: No blocked candidates executed without override
        blocked_candidates = {
            c.get("candidate_id")
            for c in candidates
            if c.get("risk_decision") == "block"
        }
        blocked_and_executed = blocked_candidates & executed_ids
        blocked_without_override = blocked_and_executed - approved_ids
        if blocked_without_override:
            compliance_flags.append(
                {
                    "check": "blocked_execution",
                    "status": "FAILED",
                    "severity": "critical",
                    "detail": f"{len(blocked_without_override)} blocked candidate(s) were executed without manual override",
                    "candidate_ids": list(blocked_without_override)[:5],
                }
            )
        else:
            checks_passed.append("No blocked candidates executed without override")

        # 3. Check: Budget compliance
        if budget_cap and budget_cap > 0:
            total_executed_amount = sum(
                er.get("amount", 0)
                for er in exec_results
                if er.get("execution_status") == "success"
            )
            if total_executed_amount > float(budget_cap):
                compliance_flags.append(
                    {
                        "check": "budget_cap",
                        "status": "FAILED",
                        "severity": "high",
                        "detail": f"Total executed (₦{total_executed_amount:,.2f}) exceeds budget cap (₦{float(budget_cap):,.2f})",
                        "over_by": round(total_executed_amount - float(budget_cap), 2),
                    }
                )
            else:
                budget_utilization = total_executed_amount / float(budget_cap) * 100
                checks_passed.append(
                    f"Within budget cap ({budget_utilization:.1f}% utilized)"
                )

        # 4. Check: Risk scoring was performed
        candidates_without_scores = [
            c for c in candidates if c.get("risk_score") is None
        ]
        if candidates_without_scores:
            compliance_flags.append(
                {
                    "check": "risk_scoring",
                    "status": "WARNING",
                    "severity": "medium",
                    "detail": f"{len(candidates_without_scores)} candidate(s) lack risk scores",
                }
            )
        else:
            checks_passed.append("All candidates have risk scores")

        # 5. Check: High-value transactions had extra scrutiny
        HIGH_VALUE_THRESHOLD = 1_000_000  # ₦1M
        high_value_candidates = [
            c for c in candidates if c.get("amount", 0) >= HIGH_VALUE_THRESHOLD
        ]
        high_value_allowed = [
            c for c in high_value_candidates if c.get("risk_decision") == "allow"
        ]
        if high_value_allowed:
            compliance_flags.append(
                {
                    "check": "high_value_review",
                    "status": "WARNING",
                    "severity": "low",
                    "detail": f"{len(high_value_allowed)} high-value (>₦1M) candidate(s) were auto-allowed - consider manual review requirement",
                }
            )

        return {
            "all_checks_passed": len(compliance_flags) == 0,
            "checks_passed": checks_passed,
            "compliance_flags": compliance_flags,
            "critical_issues": sum(
                1 for f in compliance_flags if f.get("severity") == "critical"
            ),
            "warnings": sum(
                1
                for f in compliance_flags
                if f.get("severity") in ("medium", "low", "high")
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: generate_recommendations — actionable suggestions
    # ────────────────────────────────────────────────────────────────────────
    async def generate_recommendations() -> dict[str, Any]:
        """Generate actionable recommendations based on all audit findings."""
        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])
        reconciliation_insights = state.get("reconciliation_insights", [])

        recommendations: list[dict[str, Any]] = []

        # Analyze failure patterns
        failed_results = [
            er for er in exec_results if er.get("execution_status") == "failed"
        ]
        if failed_results:
            failure_rate = len(failed_results) / max(len(exec_results), 1)
            if failure_rate > 0.1:
                recommendations.append(
                    {
                        "category": "execution",
                        "priority": "high",
                        "issue": f"High failure rate ({failure_rate:.1%})",
                        "recommendation": "Review failed transactions for common patterns (invalid accounts, insufficient funds, bank issues)",
                        "expected_impact": "Reduce failed payment costs and improve execution efficiency",
                    }
                )

        # Analyze risk scoring patterns
        scores = [
            c.get("risk_score", 0)
            for c in candidates
            if c.get("risk_score") is not None
        ]
        if scores:
            avg_score = sum(scores) / len(scores)
            if avg_score > 0.5:
                recommendations.append(
                    {
                        "category": "risk",
                        "priority": "medium",
                        "issue": f"Average risk score is high ({avg_score:.2f})",
                        "recommendation": "Review beneficiary data quality - high scores may indicate incomplete or inconsistent data",
                        "expected_impact": "Reduce false positives and improve processing speed",
                    }
                )

            # Check for score clustering
            allow_scores = [
                c.get("risk_score", 0)
                for c in candidates
                if c.get("risk_decision") == "allow"
            ]
            review_scores = [
                c.get("risk_score", 0)
                for c in candidates
                if c.get("risk_decision") == "review"
            ]
            if allow_scores and review_scores:
                if max(allow_scores) > 0.25:
                    recommendations.append(
                        {
                            "category": "risk_calibration",
                            "priority": "medium",
                            "issue": "Some 'allow' decisions have relatively high scores",
                            "recommendation": "Consider lowering the allow threshold for stricter risk control",
                            "expected_impact": "Reduce potential fraud exposure",
                        }
                    )

        # Analyze reconciliation insights
        critical_insights = [
            i
            for i in reconciliation_insights
            if i.get("status") in ("critical", "warning")
        ]
        if critical_insights:
            recommendations.append(
                {
                    "category": "reconciliation",
                    "priority": "high",
                    "issue": f"{len(critical_insights)} critical/warning issue(s) from reconciliation",
                    "recommendation": "Address reconciliation findings before next run",
                    "expected_impact": "Improve data quality and reduce anomalies",
                }
            )

        # Process efficiency
        if len(exec_results) > 0:
            success_count = sum(
                1 for er in exec_results if er.get("execution_status") == "success"
            )
            if success_count == len(exec_results):
                recommendations.append(
                    {
                        "category": "positive",
                        "priority": "info",
                        "issue": "Perfect execution success rate",
                        "recommendation": "Current process is working well - maintain current practices",
                        "expected_impact": "N/A",
                    }
                )

        # Sort by priority
        priority_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
        recommendations.sort(
            key=lambda r: priority_order.get(r.get("priority", "info"), 99)
        )

        return {
            "recommendations": recommendations,
            "total_recommendations": len(recommendations),
            "high_priority_count": sum(
                1 for r in recommendations if r.get("priority") == "high"
            ),
        }

    return [
        Tool(
            name="get_run_timeline",
            description="Get the complete timeline of events, plan steps, and reasoning for this run.",
            parameters=[],
            execute=get_run_timeline,
        ),
        Tool(
            name="compute_risk_distribution",
            description="Analyze risk scoring patterns: distribution of decisions, score statistics, high-risk candidates.",
            parameters=[],
            execute=compute_risk_distribution,
        ),
        # ── NEW INTELLIGENT AUDIT TOOLS ──
        Tool(
            name="analyze_risk_decisions",
            description="Audit risk decisions against execution outcomes. Find false negatives (allowed but failed) and evaluate manual overrides.",
            parameters=[],
            execute=analyze_risk_decisions,
        ),
        Tool(
            name="compute_cost_analysis",
            description="Calculate total fees, failed payment costs, retry costs, and efficiency metrics.",
            parameters=[],
            execute=compute_cost_analysis,
        ),
        Tool(
            name="check_compliance",
            description="Verify all required approvals happened, budget compliance, and audit trail completeness.",
            parameters=[],
            execute=check_compliance,
        ),
        Tool(
            name="compare_to_past_runs",
            description="Compare this run's metrics to historical runs for the same business.",
            parameters=[],
            execute=compare_to_past_runs,
        ),
        Tool(
            name="detect_run_anomalies",
            description="Detect anomalies in the run: blocked-but-approved candidates, high failure rates, name mismatches, etc.",
            parameters=[],
            execute=detect_run_anomalies,
        ),
        Tool(
            name="generate_recommendations",
            description="Generate actionable recommendations based on all audit findings, categorized by priority.",
            parameters=[],
            execute=generate_recommendations,
        ),
        Tool(
            name="generate_executive_summary",
            description="Compile all run metrics into a structured summary with data integrity hash.",
            parameters=[],
            execute=generate_executive_summary,
        ),
    ]


class AuditAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("AuditAgent")

    async def run(self, state: AgentState, db_session=None) -> AgentState:
        logger.info(
            f"[AuditAgent] Generating audit report for run {state.get('run_id')}"
        )

        self.registry = ToolRegistry()
        for tool in _build_audit_tools(state, db_session):
            self.registry.register(tool)

        user_prompt = f"""Generate a comprehensive audit report for this FlowPilot run.

Run ID: {state.get("run_id")}
Objective: {state.get("objective")}

Use your tools in this order:
1. Get the run timeline to understand what happened
2. Analyze risk scoring distribution
3. Use analyze_risk_decisions to audit risk decisions against actual outcomes
4. Use compute_cost_analysis to calculate fees and efficiency metrics
5. Use check_compliance to verify approvals and compliance requirements
6. Compare to past runs (if DB available)
7. Detect any anomalies in the run
8. Use generate_recommendations to produce actionable suggestions
9. Generate executive summary with all metrics

Then produce the final audit report JSON with all findings, costs, compliance status, and recommendations."""

        try:
            await self.emit_progress(
                "Analyzing run data and generating audit report..."
            )

            response = await self.reason_and_act_json(
                system_prompt=AUDIT_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

            try:
                report = json.loads(response)
            except json.JSONDecodeError:
                report = {"audit_report": {"raw_response": response}}

            if "audit_report" in report:
                audit_report = report["audit_report"]
            else:
                audit_report = report

            state_hash = hashlib.sha256(
                json.dumps(state, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]
            audit_report["data_integrity_hash"] = state_hash
            audit_report["generated_at"] = datetime.utcnow().isoformat()

            logger.info("[AuditAgent] Audit report generated with tool-based analysis")

            return {
                **state,
                "audit_report": audit_report,
                "current_step": "audit_complete",
                "audit_entries": [
                    {
                        "agent_type": "audit",
                        "action": "final_report",
                        "detail": {
                            k: v for k, v in audit_report.items() if k != "audit_trail"
                        },
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }
        except Exception as e:
            logger.error(f"[AuditAgent] Failed: {e}", exc_info=True)
            return {
                **state,
                "error": f"AuditAgent failed: {str(e)}",
                "current_step": "audit_failed",
                "audit_entries": [
                    {
                        "agent_type": "audit",
                        "action": "audit_failed",
                        "detail": {"error": str(e)},
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }

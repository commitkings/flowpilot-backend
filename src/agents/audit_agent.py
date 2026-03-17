import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry

logger = logging.getLogger(__name__)

AUDIT_SYSTEM_PROMPT = """You are a financial audit and compliance analyst for FlowPilot.

Your job: analyze the complete run data, detect anomalies, compare to historical patterns, and produce a thorough audit report.

## Your workflow:
1. Use `get_run_timeline` to understand the full sequence of events in this run
2. Use `compute_risk_distribution` to analyze the risk scoring patterns
3. Use `compare_to_past_runs` to see how this run compares to historical norms
4. Use `detect_run_anomalies` to flag anything unusual
5. Use `generate_executive_summary` to compile all findings into a structured report

## What makes a GOOD audit report:
- Quantitative facts, not vague statements
- Clear risk/compliance flags if any
- Comparison to baselines/norms
- Specific recommendations if issues found
- Data integrity verification

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
      "flagged_items": []
    },
    "compliance_flags": [],
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

Use your tools to:
1. Get the run timeline to understand what happened
2. Analyze risk scoring distribution
3. Compare to past runs (if DB available)
4. Detect any anomalies or compliance issues
5. Generate executive summary with metrics

Then produce the final audit report JSON."""

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

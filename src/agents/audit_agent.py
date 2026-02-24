import json
import hashlib
import logging
from datetime import datetime

from src.agents.base import BaseAgent
from src.agents.state import AgentState

logger = logging.getLogger(__name__)


class AuditAgent(BaseAgent):

    def __init__(self) -> None:
        super().__init__("AuditAgent")

    async def run(self, state: AgentState) -> AgentState:
        logger.info(f"[AuditAgent] Generating audit report for run {state.get('run_id')}")

        try:
            audit_entries = state.get("audit_entries", [])

            report = {
                "run_id": state.get("run_id"),
                "objective": state.get("objective"),
                "generated_at": datetime.utcnow().isoformat(),
                "plan_steps": state.get("plan_steps", []),
                "reconciliation_summary": {
                    "total_transactions": len(state.get("transactions", [])),
                    "ledger": state.get("reconciled_ledger", {}),
                    "unresolved_count": len(state.get("unresolved_references", [])),
                },
                "risk_summary": self._summarize_risk(state.get("scored_candidates", [])),
                "execution_summary": self._summarize_execution(state),
                "approval_summary": {
                    "approved": len(state.get("approved_candidate_ids", [])),
                    "rejected": len(state.get("rejected_candidate_ids", [])),
                },
                "audit_trail": audit_entries,
                "data_integrity": {
                    "state_hash": hashlib.sha256(
                        json.dumps(state, sort_keys=True, default=str).encode()
                    ).hexdigest()[:16],
                },
            }

            summary_prompt = f"""Generate a concise 3-paragraph executive summary for this FlowPilot run:

Run ID: {state.get('run_id')}
Objective: {state.get('objective')}
Transactions processed: {len(state.get('transactions', []))}
Candidates scored: {len(state.get('scored_candidates', []))}
Payouts executed: {len(state.get('candidate_execution_results', []))}
Risk summary: {json.dumps(report['risk_summary'])}

Write in professional, factual tone suitable for an audit report."""

            narrative = await self.llm_call(
                system_prompt="You are a financial audit report writer. Be factual and concise.",
                user_prompt=summary_prompt,
            )

            report["executive_summary"] = narrative

            logger.info("[AuditAgent] Audit report generated")

            return {
                **state,
                "audit_report": report,
                "current_step": "audit_complete",
                "audit_entries": [{
                    "agent_type": "audit",
                    "action": "report_generated",
                    "detail": {"report_sections": list(report.keys())},
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }
        except Exception as e:
            logger.error(f"[AuditAgent] Failed: {e}")
            return {
                **state,
                "error": f"AuditAgent failed: {str(e)}",
                "current_step": "audit_failed",
                "audit_entries": [{
                    "agent_type": "audit",
                    "action": "audit_failed",
                    "detail": {"error": str(e)},
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }

    def _summarize_risk(self, candidates: list[dict]) -> dict:
        if not candidates:
            return {"total": 0}

        decisions = {}
        total_amount = 0.0
        for c in candidates:
            d = c.get("risk_decision", "unknown")
            decisions[d] = decisions.get(d, 0) + 1
            total_amount += c.get("amount", 0.0)

        avg_score = sum(c.get("risk_score", 0) for c in candidates) / max(1, len(candidates))

        return {
            "total": len(candidates),
            "decisions": decisions,
            "average_risk_score": round(avg_score, 3),
            "total_amount": total_amount,
        }

    def _summarize_execution(self, state: AgentState) -> dict:
        lookup_results = state.get("candidate_lookup_results", [])
        exec_results = state.get("candidate_execution_results", [])
        batch = state.get("batch_details")

        lookup_counts = {}
        for lr in lookup_results:
            s = lr.get("lookup_status", "unknown")
            lookup_counts[s] = lookup_counts.get(s, 0) + 1

        exec_counts = {}
        for er in exec_results:
            s = er.get("execution_status", "unknown")
            exec_counts[s] = exec_counts.get(s, 0) + 1

        return {
            "lookups_performed": len(lookup_results),
            "lookup_statuses": lookup_counts,
            "candidates_submitted": len(exec_results),
            "execution_statuses": exec_counts,
            "batch": {
                "batch_reference": batch.get("batch_reference"),
                "submission_status": batch.get("submission_status"),
                "item_count": batch.get("item_count"),
                "accepted_count": batch.get("accepted_count"),
                "rejected_count": batch.get("rejected_count"),
            } if batch else None,
        }

import json
import logging
from datetime import datetime

from src.agents.base import BaseAgent
from src.agents.state import AgentState

logger = logging.getLogger(__name__)

RISK_SYSTEM_PROMPT = """You are a financial risk scoring engine for FlowPilot.

Given a list of payout candidates and transaction context, score each candidate for risk.

For each candidate, evaluate:
1. Amount deviation: Is the amount unusually high compared to typical transactions?
2. Duplicate detection: Does this look like a duplicate of another candidate?
3. Beneficiary anomalies: Any red flags on the account/institution?
4. Timing outliers: Unusual timing patterns?

Return a JSON object:
{
  "scored_candidates": [
    {
      "candidate_id": "...",
      "beneficiary_name": "...",
      "institution_code": "...",
      "account_number": "...",
      "amount": 0.0,
      "risk_score": 0.0,
      "risk_reasons": ["reason1", "reason2"],
      "risk_decision": "allow|review|block"
    }
  ]
}

Risk thresholds:
- 0.0 - 0.3: allow (safe to auto-approve)
- 0.3 - 0.6: review (requires human review)
- 0.6 - 1.0: block (auto-reject)

Be conservative. When in doubt, flag for review.
"""


class RiskAgent(BaseAgent):

    def __init__(self) -> None:
        super().__init__("RiskAgent")

    async def run(self, state: AgentState) -> AgentState:
        candidates = state.get("scored_candidates", [])
        risk_tolerance = state.get("risk_tolerance", 0.35)
        ledger = state.get("reconciled_ledger", {})

        logger.info(f"[RiskAgent] Scoring {len(candidates)} candidates (tolerance: {risk_tolerance})")

        if not candidates:
            logger.warning("[RiskAgent] No candidates to score")
            return {
                **state,
                "current_step": "risk_complete",
                "audit_entries": [{
                    "agent_type": "risk",
                    "action": "risk_skipped",
                    "detail": {"reason": "no candidates"},
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }

        try:
            await self.emit_progress(f"Scoring {len(candidates)} candidates...", {
                "candidate_count": len(candidates),
                "risk_tolerance": risk_tolerance,
            })

            user_prompt = f"""Payout candidates to score:
{json.dumps(candidates, indent=2)}

Transaction context (reconciled ledger):
{json.dumps(ledger, indent=2)}

Risk tolerance threshold: {risk_tolerance}"""

            response = await self.llm_json_call(
                system_prompt=RISK_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

            result = json.loads(response)
            llm_scored = result.get("scored_candidates", [])

            # Merge LLM risk scores onto original candidates (preserves candidate_id
            # and other fields the LLM may have dropped from its response)
            for i, orig in enumerate(candidates):
                if i < len(llm_scored):
                    orig["risk_score"] = llm_scored[i].get("risk_score", 0.5)
                    orig["risk_reasons"] = llm_scored[i].get("risk_reasons", [])
                else:
                    orig.setdefault("risk_score", 0.5)
                    orig.setdefault("risk_reasons", [])
            scored = candidates

            for c in scored:
                score = c.get("risk_score", 0.5)
                if score <= 0.3:
                    c["risk_decision"] = "allow"
                elif score <= 0.6:
                    c["risk_decision"] = "review"
                else:
                    c["risk_decision"] = "block"

            allow_count = sum(1 for c in scored if c.get("risk_decision") == "allow")
            review_count = sum(1 for c in scored if c.get("risk_decision") == "review")
            block_count = sum(1 for c in scored if c.get("risk_decision") == "block")

            logger.info(f"[RiskAgent] Results: allow={allow_count}, review={review_count}, block={block_count}")

            return {
                **state,
                "scored_candidates": scored,
                "current_step": "risk_complete",
                "audit_entries": [{
                    "agent_type": "risk",
                    "action": "risk_scoring_complete",
                    "detail": {
                        "total_scored": len(scored),
                        "allow": allow_count,
                        "review": review_count,
                        "block": block_count,
                    },
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }
        except Exception as e:
            logger.error(f"[RiskAgent] Failed: {e}")
            return {
                **state,
                "error": f"RiskAgent failed: {str(e)}",
                "current_step": "risk_failed",
                "audit_entries": [{
                    "agent_type": "risk",
                    "action": "risk_failed",
                    "detail": {"error": str(e)},
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }

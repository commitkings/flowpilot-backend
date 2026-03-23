import json
import logging
from datetime import datetime

from src.agents.base import BaseAgent
from src.agents.state import AgentState

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are a financial operations planner for FlowPilot, a multi-agent fintech execution system.

Given an operator objective, decompose it into an executable plan with ordered tasks.

Available agents:
- reconciliation: Pull and normalize transaction data, detect mismatches
- risk: Score payout candidates for risk (duplicates, anomalies, amount spikes)
- forecast: Predict short-term liquidity and payout feasibility
- execution: Verify beneficiaries and execute approved payouts
- audit: Generate audit trail and run report

Return a JSON object with this structure:
{
  "plan_steps": [
    {
      "step_id": "step_1",
      "agent_type": "reconciliation|risk|forecast|execution|audit",
      "order": 1,
      "description": "What this step does",
      "depends_on": []
    }
  ],
  "summary": "Brief plan summary"
}

Rules:
- Always start with reconciliation if transaction data is needed
- Risk scoring must come before execution
- Audit is always the last step
- Include forecast only if the objective mentions liquidity, cash position, or budget feasibility
- Order steps logically with proper dependencies
"""


class PlannerAgent(BaseAgent):

    def __init__(self) -> None:
        super().__init__("PlannerAgent")

    async def run(self, state: AgentState) -> AgentState:
        logger.info(f"[PlannerAgent] Planning for objective: {state['objective'][:100]}")

        user_prompt = f"""Objective: {state['objective']}
Constraints: {state.get('constraints', 'None')}
Risk tolerance: {state.get('risk_tolerance', 0.35)}
Budget cap: {state.get('budget_cap', 'No limit')}"""

        try:
            response = await self.llm_json_call(
                system_prompt=PLANNER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
            plan = json.loads(response)
            steps = plan.get("plan_steps", [])

            logger.info(f"[PlannerAgent] Generated plan with {len(steps)} steps")

            return {
                **state,
                "plan_steps": steps,
                "current_step": "planning_complete",
                "audit_entries": [{
                    "agent_type": "planner",
                    "action": "plan_generated",
                    "detail": {"step_count": len(steps), "summary": plan.get("summary", "")},
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }
        except Exception as e:
            logger.error(f"[PlannerAgent] Failed: {e}")
            return {
                **state,
                "error": f"PlannerAgent failed: {str(e)}",
                "current_step": "planning_failed",
                "audit_entries": [{
                    "agent_type": "planner",
                    "action": "plan_failed",
                    "detail": {"error": str(e)},
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }

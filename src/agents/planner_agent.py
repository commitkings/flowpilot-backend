import json
import logging
from datetime import datetime
from typing import Any

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are a financial operations planner for FlowPilot, a multi-agent fintech execution system.

Your job: given an operator objective, USE YOUR TOOLS to gather information, then produce an execution plan.

## Available pipeline agents you can include in your plan:
- reconciliation: Pull and normalize transaction data from Interswitch, detect mismatches
- risk: Score payout candidates for risk (duplicates, anomalies, amount spikes)
- execution: Verify beneficiaries and execute approved payouts via Interswitch
- audit: Generate audit trail and run report

## Your workflow:
1. Use `check_available_data` to understand what data is already available in the current run state
2. Use `query_business_config` to understand the business's risk appetite and preferences
3. Use `check_wallet_balance` to check available funds (if the objective involves payouts)
4. Use `get_recent_runs` to learn from past run outcomes
5. Based on what you learn, produce a plan

## Plan output format (your final answer must be this JSON):
{
  "plan_steps": [
    {
      "step_id": "step_1",
      "agent_type": "reconciliation|risk|execution|audit",
      "order": 1,
      "description": "What this step does and WHY based on data gathered",
      "depends_on": [],
      "config_overrides": {}
    }
  ],
  "summary": "Brief plan summary explaining reasoning",
  "risk_assessment": "Pre-execution risk notes based on wallet balance, past runs, business config",
  "estimated_candidates": 0
}

## Rules:
- Always start with reconciliation if transaction data is needed
- Risk scoring must come before execution
- Audit is always the last step
- Order steps logically with proper dependencies
- The plan MUST reflect real data from your tools, not generic templates
- If wallet balance is low relative to expected payouts, note this in risk_assessment
- If past runs had high failure rates, adjust the plan accordingly
"""


def _build_planner_tools(state: AgentState, db_session=None) -> list[Tool]:
    async def check_available_data() -> dict[str, Any]:
        available = {
            "has_objective": bool(state.get("objective")),
            "objective": state.get("objective", ""),
            "has_constraints": bool(state.get("constraints")),
            "constraints": state.get("constraints", ""),
            "has_date_range": bool(state.get("date_from") and state.get("date_to")),
            "date_from": state.get("date_from"),
            "date_to": state.get("date_to"),
            "risk_tolerance": state.get("risk_tolerance", 0.35),
            "budget_cap": state.get("budget_cap"),
            "merchant_id": state.get("merchant_id", ""),
            "has_existing_transactions": bool(state.get("transactions")),
            "transaction_count": len(state.get("transactions", [])),
            "has_existing_candidates": bool(state.get("scored_candidates")),
            "candidate_count": len(state.get("scored_candidates", [])),
        }
        return available

    async def query_business_config() -> dict[str, Any]:
        if db_session is None:
            return {
                "error": "No database session available",
                "fallback": {
                    "risk_tolerance": state.get("risk_tolerance", 0.35),
                    "budget_cap": state.get("budget_cap"),
                },
            }

        try:
            from src.infrastructure.database.repositories.business_repository import (
                BusinessRepository,
            )

            repo = BusinessRepository(db_session)
            from uuid import UUID

            business_id = state.get("business_id")
            if not business_id:
                return {"error": "No business_id in state"}

            bid = UUID(business_id) if isinstance(business_id, str) else business_id
            biz = await repo.get_by_id(bid)
            if biz is None:
                return {"error": f"Business {business_id} not found"}

            from sqlalchemy import select
            from src.infrastructure.database.flowpilot_models import BusinessConfigModel

            result = await db_session.execute(
                select(BusinessConfigModel).where(
                    BusinessConfigModel.business_id == bid
                )
            )
            config = result.scalar_one_or_none()

            return {
                "business_name": biz.business_name,
                "business_type": biz.business_type,
                "config": {
                    "monthly_txn_volume_range": config.monthly_txn_volume_range
                    if config
                    else None,
                    "avg_monthly_payouts_range": config.avg_monthly_payouts_range
                    if config
                    else None,
                    "primary_bank": config.primary_bank if config else None,
                    "primary_use_cases": config.primary_use_cases if config else None,
                    "risk_appetite": config.risk_appetite if config else None,
                    "default_risk_tolerance": float(config.default_risk_tolerance)
                    if config and config.default_risk_tolerance
                    else 0.35,
                    "default_budget_cap": float(config.default_budget_cap)
                    if config and config.default_budget_cap
                    else None,
                    "preferences": config.preferences if config else {},
                }
                if config
                else {"note": "No config found, using defaults"},
            }
        except Exception as e:
            logger.error(f"query_business_config failed: {e}", exc_info=True)
            return {"error": str(e)}

    async def check_wallet_balance() -> dict[str, Any]:
        try:
            from src.infrastructure.external_services.interswitch.payouts import (
                PayoutClient,
            )
            from src.config.settings import Settings

            if Settings.PAYOUT_MODE == "simulated":
                return {
                    "mode": "simulated",
                    "available_balance": 5000000.00,
                    "ledger_balance": 5000000.00,
                    "currency": "NGN",
                    "note": "Simulated balance — not real",
                }
            client = PayoutClient()
            balance_data = await client.get_wallet_balance()
            return {
                "mode": "live",
                "available_balance": balance_data.get("availableBalance"),
                "ledger_balance": balance_data.get("ledgerBalance"),
                "currency": "NGN",
            }
        except Exception as e:
            logger.error(f"check_wallet_balance failed: {e}", exc_info=True)
            return {
                "error": str(e),
                "note": "Could not check balance — plan conservatively",
            }

    async def get_recent_runs(limit: int = 5) -> dict[str, Any]:
        if db_session is None:
            return {"error": "No database session available", "runs": []}

        try:
            from src.infrastructure.database.repositories.run_repository import (
                RunRepository,
            )
            from uuid import UUID

            repo = RunRepository(db_session)
            business_id = state.get("business_id")
            if not business_id:
                return {"error": "No business_id in state", "runs": []}

            bid = UUID(business_id) if isinstance(business_id, str) else business_id
            runs, total = await repo.list_by_business(bid, limit=limit)

            run_summaries = []
            for r in runs:
                run_summaries.append(
                    {
                        "run_id": str(r.id),
                        "status": r.status,
                        "objective": r.objective[:100] if r.objective else "",
                        "created_at": r.created_at.isoformat()
                        if r.created_at
                        else None,
                        "risk_tolerance": float(r.risk_tolerance)
                        if r.risk_tolerance
                        else None,
                        "budget_cap": float(r.budget_cap) if r.budget_cap else None,
                        "error_message": r.error_message[:200]
                        if r.error_message
                        else None,
                    }
                )

            completed = sum(1 for r in runs if r.status == "completed")
            failed = sum(1 for r in runs if r.status == "failed")

            return {
                "total_runs": total,
                "recent_runs": run_summaries,
                "stats": {
                    "completed": completed,
                    "failed": failed,
                    "success_rate": round(completed / max(len(runs), 1), 2),
                },
            }
        except Exception as e:
            logger.error(f"get_recent_runs failed: {e}", exc_info=True)
            return {"error": str(e), "runs": []}

    return [
        Tool(
            name="check_available_data",
            description="Check what data is already available in the current run state (objective, date range, existing transactions, candidates, etc.)",
            parameters=[],
            execute=check_available_data,
        ),
        Tool(
            name="query_business_config",
            description="Query the business profile and configuration including risk appetite, transaction volumes, use cases, and budget limits.",
            parameters=[],
            execute=query_business_config,
        ),
        Tool(
            name="check_wallet_balance",
            description="Check the current Interswitch wallet balance to verify available funds for payouts.",
            parameters=[],
            execute=check_wallet_balance,
        ),
        Tool(
            name="get_recent_runs",
            description="Retrieve recent agent run history for this business to learn from past outcomes (success/failure rates, common patterns).",
            parameters=[
                ToolParam(
                    name="limit",
                    param_type=ToolParamType.INTEGER,
                    description="Maximum number of recent runs to retrieve (default 5)",
                    required=False,
                    default=5,
                ),
            ],
            execute=get_recent_runs,
        ),
    ]


class PlannerAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("PlannerAgent")

    def _register_tools(self, state: AgentState, db_session=None) -> None:
        self.registry = ToolRegistry()
        for tool in _build_planner_tools(state, db_session):
            self.registry.register(tool)

    async def run(self, state: AgentState, db_session=None) -> AgentState:
        logger.info(
            f"[PlannerAgent] Planning for objective: {state.get('objective', '')[:100]}"
        )

        self._register_tools(state, db_session)

        user_prompt = f"""I need to create an execution plan for the following objective:

Objective: {state.get("objective", "Not specified")}
Constraints: {state.get("constraints", "None")}
Risk tolerance: {state.get("risk_tolerance", 0.35)}
Budget cap: {state.get("budget_cap", "No limit")}
Date range: {state.get("date_from", "Not set")} to {state.get("date_to", "Not set")}

Use your tools to gather the information you need, then produce the execution plan."""

        try:
            await self.emit_progress("Gathering data and generating execution plan...")

            response = await self.reason_and_act_json(
                system_prompt=PLANNER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
            plan = json.loads(response)
            steps = plan.get("plan_steps", [])

            logger.info(
                f"[PlannerAgent] Generated plan with {len(steps)} steps after tool-based analysis"
            )

            return {
                **state,
                "plan_steps": steps,
                "current_step": "planning_complete",
                "audit_entries": [
                    {
                        "agent_type": "planner",
                        "action": "plan_generated",
                        "detail": {
                            "step_count": len(steps),
                            "summary": plan.get("summary", ""),
                            "risk_assessment": plan.get("risk_assessment", ""),
                            "estimated_candidates": plan.get("estimated_candidates", 0),
                            "tools_used": self.registry.tool_names,
                        },
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }
        except Exception as e:
            logger.error(f"[PlannerAgent] Failed: {e}", exc_info=True)
            return {
                **state,
                "error": f"PlannerAgent failed: {str(e)}",
                "current_step": "planning_failed",
                "audit_entries": [
                    {
                        "agent_type": "planner",
                        "action": "plan_failed",
                        "detail": {"error": str(e)},
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }

import json
import logging
from datetime import datetime
from typing import Any

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry
from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.gateway_factory import (
    get_payout_gateway,
)
from src.infrastructure.external_services.interswitch.payout_gateway import (
    PayoutGateway,
)
from src.utilities.name_match import name_match_score, NAME_MATCH_THRESHOLD

logger = logging.getLogger(__name__)

EXECUTION_SYSTEM_PROMPT = """You are the payout execution agent for FlowPilot.

Your job: safely execute approved payouts by verifying beneficiaries and processing payments.

## CRITICAL SAFETY RULES:
- ONLY execute payouts for candidates in the approved list
- NEVER skip verification — every candidate MUST pass customer lookup before payout
- If wallet balance is insufficient, STOP and report the issue
- If verification failure rate > 50%, STOP and flag for human review

## Your workflow:
1. Use `run_preflight_checks` to verify wallet balance and approved candidate count
2. Use `verify_beneficiary` for EACH approved candidate (validates account + name matching)
3. Review verification results — decide whether to proceed based on failure rate
4. Use `execute_single_payout` for each VERIFIED candidate
5. Use `check_payout_status` to poll final status for pending payouts

## Decision points where you must reason:
- After preflight: Is wallet balance sufficient for total payout amount?
- After verification: What % of candidates passed? Should we proceed or abort?
- After each payout: If failures occur, should we continue or stop?
- Name mismatches: Flag for review but don't auto-reject (names can vary)

## Final answer format (JSON):
{
  "execution_summary": {
    "total_approved": 0,
    "verified_count": 0,
    "executed_count": 0,
    "success_count": 0,
    "failed_count": 0,
    "pending_count": 0,
    "total_amount_executed": 0.0,
    "wallet_balance_before": 0.0,
    "decision_log": ["Reasoning about each key decision made during execution"]
  }
}
"""


def _build_execution_tools(
    state: AgentState, gateway: PayoutGateway
) -> tuple[list[Tool], dict[str, Any]]:
    approved_ids = set(state.get("approved_candidate_ids", []))
    candidates = state.get("scored_candidates", [])
    approved_candidates = [
        c for c in candidates if c.get("candidate_id") in approved_ids
    ]

    prior_results = state.get("candidate_execution_results", [])
    submitted_statuses = {"pending", "success", "failed"}
    already_executed = {
        er["candidate_id"]
        for er in prior_results
        if er.get("execution_status") in submitted_statuses
    }
    approved_candidates = [
        c for c in approved_candidates if c.get("candidate_id") not in already_executed
    ]

    run_id = state.get("run_id", "unknown")

    shared_data: dict[str, Any] = {
        "lookup_results": [],
        "execution_results": list(prior_results),
        "batch_details": None,
        "verified_candidates": [],
    }

    async def run_preflight_checks() -> dict[str, Any]:
        total_amount = sum(c.get("amount", 0) for c in approved_candidates)
        payout_mode = "simulated" if gateway.is_simulated else "live"

        wallet_info = {"note": "Wallet check skipped in simulated mode"}
        if not gateway.is_simulated:
            try:
                from src.infrastructure.external_services.interswitch.payouts import (
                    PayoutClient,
                )

                client = PayoutClient()
                balance_data = await client.get_wallet_balance()
                wallet_info = {
                    "available_balance": balance_data.get("availableBalance"),
                    "ledger_balance": balance_data.get("ledgerBalance"),
                    "sufficient": (balance_data.get("availableBalance") or 0)
                    >= total_amount,
                }
            except Exception as e:
                wallet_info = {
                    "error": str(e),
                    "note": "Could not verify balance — proceed with caution",
                }

        return {
            "payout_mode": payout_mode,
            "total_approved_candidates": len(approved_candidates),
            "already_executed_count": len(already_executed),
            "total_payout_amount": total_amount,
            "wallet": wallet_info,
            "candidates": [
                {
                    "candidate_id": c.get("candidate_id"),
                    "beneficiary_name": c.get("beneficiary_name"),
                    "account_number": c.get("account_number"),
                    "institution_code": c.get("institution_code"),
                    "amount": c.get("amount"),
                    "risk_score": c.get("risk_score"),
                    "risk_decision": c.get("risk_decision"),
                }
                for c in approved_candidates
            ],
        }

    async def verify_beneficiary(candidate_id: str) -> dict[str, Any]:
        candidate = None
        for c in approved_candidates:
            if c.get("candidate_id") == candidate_id:
                candidate = c
                break
        if candidate is None:
            return {"error": f"Candidate {candidate_id} not found in approved list"}

        txn_ref = f"FP_{run_id}_{candidate_id}"

        try:
            raw = await gateway.lookup_customer(
                institution_code=candidate["institution_code"],
                account_number=candidate["account_number"],
                transaction_reference=txn_ref,
            )

            if raw.get("lookupStatus") != "SUCCESS":
                result = {
                    "candidate_id": candidate_id,
                    "lookup_status": "failed",
                    "lookup_account_name": None,
                    "lookup_match_score": None,
                    "institution_code": candidate.get("institution_code", ""),
                    "transaction_reference": None,
                    "raw_response": raw,
                    "reason": f"Lookup returned status: {raw.get('lookupStatus')}",
                }
                shared_data["lookup_results"].append(result)
                return result

            lookup_name = raw.get("accountName", "")
            beneficiary_name = candidate.get("beneficiary_name", "")
            match_score = name_match_score(lookup_name, beneficiary_name)

            if match_score >= NAME_MATCH_THRESHOLD:
                lookup_status = "success"
                candidate["_transaction_reference"] = txn_ref
                shared_data["verified_candidates"].append(candidate)
            else:
                lookup_status = "mismatch"

            result = {
                "candidate_id": candidate_id,
                "lookup_status": lookup_status,
                "lookup_account_name": lookup_name,
                "beneficiary_name": beneficiary_name,
                "lookup_match_score": round(match_score, 3),
                "match_threshold": NAME_MATCH_THRESHOLD,
                "institution_code": candidate.get("institution_code", ""),
                "transaction_reference": txn_ref,
                "raw_response": raw,
            }
            shared_data["lookup_results"].append(result)
            return result

        except Exception as e:
            result = {
                "candidate_id": candidate_id,
                "lookup_status": "failed",
                "lookup_account_name": None,
                "lookup_match_score": None,
                "institution_code": candidate.get("institution_code", ""),
                "error": str(e),
                "transaction_reference": None,
                "raw_response": {},
            }
            shared_data["lookup_results"].append(result)
            return result

    async def execute_single_payout(candidate_id: str) -> dict[str, Any]:
        candidate = None
        for c in shared_data["verified_candidates"]:
            if c.get("candidate_id") == candidate_id:
                candidate = c
                break
        if candidate is None:
            return {
                "error": f"Candidate {candidate_id} not found in verified list. Verify first."
            }

        txn_ref = candidate.get("_transaction_reference")
        if not txn_ref:
            return {
                "error": f"No transaction reference for {candidate_id}. Verify first."
            }

        batch_ref = f"FP_{run_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        try:
            items = [
                {
                    "client_reference": candidate_id,
                    "amount": candidate["amount"],
                    "institution_code": candidate["institution_code"],
                    "account_number": candidate["account_number"],
                    "narration": candidate.get("purpose", "FlowPilot payout"),
                    "transaction_reference": txn_ref,
                }
            ]

            raw = await gateway.execute_payout(
                batch_reference=batch_ref,
                items=items,
            )

            resp_items = raw.get("items", [])
            resp_item = resp_items[0] if resp_items else {}
            item_status = (resp_item.get("status") or "").upper()
            provider_ref = resp_item.get("providerReference", "")

            exec_status = "pending" if item_status in ("PENDING", "") else "failed"

            exec_result = {
                "candidate_id": candidate_id,
                "execution_status": exec_status,
                "client_reference": candidate_id,
                "provider_reference": provider_ref,
                "response_message": resp_item.get("responseMessage", ""),
                "amount": candidate["amount"],
                "batch_reference": batch_ref,
            }
            shared_data["execution_results"].append(exec_result)

            submission_status = raw.get("submissionStatus", "pending")
            accepted = raw.get("acceptedCount", 0) or 0
            rejected = raw.get("rejectedCount", 0) or 0

            if shared_data["batch_details"] is None:
                shared_data["batch_details"] = {
                    "batch_reference": batch_ref,
                    "currency": candidate.get("currency", "NGN"),
                    "source_account_id": Settings.INTERSWITCH_WALLET_ID or "",
                    "total_amount": float(candidate["amount"]),
                    "item_count": 1,
                    "submission_status": submission_status,
                    "accepted_count": accepted,
                    "rejected_count": rejected,
                }
            else:
                bd = shared_data["batch_details"]
                bd["total_amount"] = bd.get("total_amount", 0) + float(
                    candidate["amount"]
                )
                bd["item_count"] = bd.get("item_count", 0) + 1
                bd["accepted_count"] = bd.get("accepted_count", 0) + accepted
                bd["rejected_count"] = bd.get("rejected_count", 0) + rejected

            return {
                "candidate_id": candidate_id,
                "execution_status": exec_status,
                "provider_reference": provider_ref,
                "amount": candidate["amount"],
                "submission_status": raw.get("submissionStatus"),
            }

        except Exception as e:
            exec_result = {
                "candidate_id": candidate_id,
                "execution_status": "failed",
                "client_reference": candidate_id,
                "provider_reference": None,
                "response_message": str(e),
                "error": str(e),
            }
            shared_data["execution_results"].append(exec_result)
            return {
                "error": str(e),
                "candidate_id": candidate_id,
                "execution_status": "failed",
            }

    async def check_payout_status(candidate_id: str) -> dict[str, Any]:
        exec_result = None
        for er in shared_data["execution_results"]:
            if er.get("candidate_id") == candidate_id:
                exec_result = er
                break
        if exec_result is None:
            return {"error": f"No execution record for {candidate_id}"}

        provider_ref = exec_result.get("provider_reference")
        if not provider_ref:
            return {
                "error": f"No provider reference for {candidate_id}",
                "execution_status": exec_result.get("execution_status"),
            }

        try:
            status_resp = await gateway.requery_payout(
                transaction_reference=provider_ref
            )
            raw_status = (status_resp.get("status") or "").upper()

            if raw_status == "SUCCESSFUL":
                exec_result["execution_status"] = "success"
            elif raw_status == "FAILED":
                exec_result["execution_status"] = "failed"

            return {
                "candidate_id": candidate_id,
                "provider_reference": provider_ref,
                "raw_status": raw_status,
                "execution_status": exec_result["execution_status"],
            }
        except Exception as e:
            return {"error": str(e), "candidate_id": candidate_id}

    tools = [
        Tool(
            name="run_preflight_checks",
            description="Check wallet balance, count approved candidates, and verify readiness for execution.",
            parameters=[],
            execute=run_preflight_checks,
        ),
        Tool(
            name="verify_beneficiary",
            description="Verify a candidate's bank account via Interswitch customer lookup + name matching. MUST be done before executing payout.",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to verify",
                ),
            ],
            execute=verify_beneficiary,
        ),
        Tool(
            name="execute_single_payout",
            description="Execute a payout for a VERIFIED candidate. Only call this after successful verification.",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to pay out (must be verified first)",
                ),
            ],
            execute=execute_single_payout,
        ),
        Tool(
            name="check_payout_status",
            description="Poll the final status of an executed payout (SUCCESSFUL/FAILED/PROCESSING).",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id whose payout status to check",
                ),
            ],
            execute=check_payout_status,
        ),
    ]

    return tools, shared_data


class ExecutionAgent(BaseAgent):
    def __init__(self, gateway: PayoutGateway | None = None) -> None:
        super().__init__("ExecutionAgent")
        self._gateway = gateway or get_payout_gateway()

    async def run(self, state: AgentState, db_session=None) -> AgentState:
        approved_ids = set(state.get("approved_candidate_ids", []))
        candidates = state.get("scored_candidates", [])
        approved_candidates = [
            c for c in candidates if c.get("candidate_id") in approved_ids
        ]

        logger.info(
            f"[ExecutionAgent] Processing {len(approved_candidates)} approved candidates"
        )
        await self.emit_progress(
            f"Processing {len(approved_candidates)} approved candidates"
        )

        if not approved_candidates:
            logger.warning("[ExecutionAgent] No approved candidates to process")
            return {
                **state,
                "candidate_lookup_results": state.get("candidate_lookup_results", []),
                "candidate_execution_results": state.get(
                    "candidate_execution_results", []
                ),
                "batch_details": state.get("batch_details", {}),
                "current_step": "execution_complete",
                "audit_entries": [
                    {
                        "agent_type": "execution",
                        "action": "execution_skipped",
                        "detail": {"reason": "no approved candidates"},
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }

        tools, shared_data = _build_execution_tools(state, self._gateway)
        self.registry = ToolRegistry()
        for tool in tools:
            self.registry.register(tool)

        payout_mode = "simulated" if self._gateway.is_simulated else "live"
        candidate_list = json.dumps(
            [
                {
                    "candidate_id": c.get("candidate_id"),
                    "beneficiary_name": c.get("beneficiary_name"),
                    "account_number": c.get("account_number"),
                    "institution_code": c.get("institution_code"),
                    "amount": c.get("amount"),
                }
                for c in approved_candidates
            ],
            indent=2,
        )

        user_prompt = f"""Execute payouts for {len(approved_candidates)} approved candidates.

Payout mode: {payout_mode}

Approved candidates:
{candidate_list}

Steps:
1. Run preflight checks (wallet balance, candidate validation)
2. Verify each candidate via customer lookup (call verify_beneficiary for each)
3. Evaluate verification results — decide whether to proceed
4. Execute payout for each verified candidate (call execute_single_payout for each)
5. Check payout status for any pending payouts (call check_payout_status)
6. Produce final execution summary as JSON"""

        try:
            response = await self.reason_and_act_json(
                system_prompt=EXECUTION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_iterations=MAX_EXEC_ITERATIONS,
            )

            try:
                summary = json.loads(response)
            except json.JSONDecodeError:
                summary = {"execution_summary": {"raw_response": response}}

            lookup_results = shared_data["lookup_results"]
            execution_results = shared_data["execution_results"]
            batch_details = shared_data["batch_details"]

            success_count = sum(
                1 for r in execution_results if r.get("execution_status") == "success"
            )
            failed_count = sum(
                1 for r in execution_results if r.get("execution_status") == "failed"
            )
            pending_count = sum(
                1 for r in execution_results if r.get("execution_status") == "pending"
            )
            total_executed = sum(
                r.get("amount", 0)
                for r in execution_results
                if r.get("execution_status") in ("success", "pending")
            )

            await self.emit_progress(
                f"Execution complete — {success_count} success, {pending_count} pending, {failed_count} failed",
                {"total_amount": total_executed},
            )

            audit_entries: list[dict] = [
                {
                    "agent_type": "execution",
                    "action": "execution_complete",
                    "detail": {
                        "payout_mode": payout_mode,
                        "verified_count": len(shared_data["verified_candidates"]),
                        "success_count": success_count,
                        "failed_count": failed_count,
                        "pending_count": pending_count,
                        "total_amount_executed": total_executed,
                        "ai_summary": summary.get("execution_summary", {}),
                    },
                    "created_at": datetime.utcnow().isoformat(),
                }
            ]

            return {
                **state,
                "candidate_lookup_results": lookup_results,
                "candidate_execution_results": execution_results,
                "batch_details": batch_details or state.get("batch_details", {}),
                "current_step": "execution_complete",
                "audit_entries": audit_entries,
            }
        except Exception as e:
            logger.error(f"[ExecutionAgent] Failed: {e}", exc_info=True)
            return {
                **state,
                "candidate_lookup_results": shared_data.get("lookup_results", []),
                "candidate_execution_results": shared_data.get("execution_results", []),
                "batch_details": None,
                "error": f"ExecutionAgent failed: {str(e)}",
                "current_step": "execution_failed",
                "audit_entries": [
                    {
                        "agent_type": "execution",
                        "action": "execution_failed",
                        "detail": {"error": str(e)},
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }


MAX_EXEC_ITERATIONS = 25

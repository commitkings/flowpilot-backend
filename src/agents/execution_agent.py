import logging
import uuid
from datetime import datetime

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.infrastructure.external_services.interswitch.customer_lookup import CustomerLookupClient
from src.infrastructure.external_services.interswitch.payouts import PayoutClient

logger = logging.getLogger(__name__)


class ExecutionAgent(BaseAgent):

    def __init__(self) -> None:
        super().__init__("ExecutionAgent")
        self._lookup_client = CustomerLookupClient()
        self._payout_client = PayoutClient()

    async def run(self, state: AgentState) -> AgentState:
        approved_ids = state.get("approved_candidate_ids", [])
        candidates = state.get("scored_candidates", [])

        approved_candidates = [
            c for c in candidates
            if c.get("candidate_id") in approved_ids
            or c.get("risk_decision") == "allow"
        ]

        logger.info(f"[ExecutionAgent] Processing {len(approved_candidates)} approved candidates")

        if not approved_candidates:
            logger.warning("[ExecutionAgent] No approved candidates to process")
            return {
                **state,
                "current_step": "execution_complete",
                "audit_entries": [{
                    "agent_type": "execution",
                    "action": "execution_skipped",
                    "detail": {"reason": "no approved candidates"},
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }

        try:
            lookup_results = await self._verify_recipients(approved_candidates)

            verified_candidates = [
                c for c, lr in zip(approved_candidates, lookup_results)
                if lr.get("lookupStatus") == "SUCCESS"
            ]

            failed_lookups = [
                c for c, lr in zip(approved_candidates, lookup_results)
                if lr.get("lookupStatus") != "SUCCESS"
            ]

            if failed_lookups:
                logger.warning(f"[ExecutionAgent] {len(failed_lookups)} candidates failed verification")

            payout_results = []
            if verified_candidates:
                payout_results = await self._execute_payouts(state["run_id"], verified_candidates)

            payout_status_results = []
            for pr in payout_results:
                for item in pr.get("items", []):
                    ref = item.get("providerReference")
                    if ref:
                        try:
                            status = await self._payout_client.get_payout_status(ref)
                            payout_status_results.append(status)
                        except Exception as e:
                            logger.warning(f"[ExecutionAgent] Status check failed for {ref}: {e}")

            return {
                **state,
                "lookup_results": lookup_results,
                "payout_results": payout_results,
                "payout_status_results": payout_status_results,
                "current_step": "execution_complete",
                "audit_entries": [{
                    "agent_type": "execution",
                    "action": "execution_complete",
                    "detail": {
                        "verified_count": len(verified_candidates),
                        "failed_lookup_count": len(failed_lookups),
                        "payout_batches": len(payout_results),
                        "status_checks": len(payout_status_results),
                    },
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }
        except Exception as e:
            logger.error(f"[ExecutionAgent] Failed: {e}")
            return {
                **state,
                "error": f"ExecutionAgent failed: {str(e)}",
                "current_step": "execution_failed",
                "audit_entries": [{
                    "agent_type": "execution",
                    "action": "execution_failed",
                    "detail": {"error": str(e)},
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }

    async def _verify_recipients(self, candidates: list[dict]) -> list[dict]:
        results = []
        for c in candidates:
            try:
                result = await self._lookup_client.lookup_customer(
                    institution_code=c["institution_code"],
                    account_number=c["account_number"],
                )
                results.append(result)
            except Exception as e:
                logger.warning(f"[ExecutionAgent] Lookup failed for {c.get('candidate_id')}: {e}")
                results.append({"lookupStatus": "FAILED", "error": str(e)})
        return results

    async def _execute_payouts(self, run_id: str, candidates: list[dict]) -> list[dict]:
        batch_reference = f"FP_{run_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        items = [
            {
                "client_reference": c.get("candidate_id", str(uuid.uuid4())),
                "amount": c["amount"],
                "institution_code": c["institution_code"],
                "account_number": c["account_number"],
                "account_name": c["beneficiary_name"],
                "narration": c.get("purpose", "FlowPilot payout"),
            }
            for c in candidates
        ]

        result = await self._payout_client.execute_payout(
            batch_reference=batch_reference,
            items=items,
        )
        return [result]

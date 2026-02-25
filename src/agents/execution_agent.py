import logging
import uuid
from datetime import datetime

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.customer_lookup import CustomerLookupClient
from src.infrastructure.external_services.interswitch.payouts import PayoutClient
from src.utilities.name_match import name_match_score, NAME_MATCH_THRESHOLD

logger = logging.getLogger(__name__)


class ExecutionAgent(BaseAgent):

    def __init__(self) -> None:
        super().__init__("ExecutionAgent")
        self._lookup_client = CustomerLookupClient()
        self._payout_client = PayoutClient()

    async def run(self, state: AgentState) -> AgentState:
        approved_ids = set(state.get("approved_candidate_ids", []))
        candidates = state.get("scored_candidates", [])

        # Only explicitly approved candidates may execute — never bypass via risk_decision
        approved_candidates = [
            c for c in candidates
            if c.get("candidate_id") in approved_ids
        ]

        logger.info(f"[ExecutionAgent] Processing {len(approved_candidates)} approved candidates")

        if not approved_candidates:
            logger.warning("[ExecutionAgent] No approved candidates to process")
            return {
                **state,
                "candidate_lookup_results": [],
                "candidate_execution_results": [],
                "batch_details": None,
                "current_step": "execution_complete",
                "audit_entries": [{
                    "agent_type": "execution",
                    "action": "execution_skipped",
                    "detail": {"reason": "no approved candidates"},
                }],
            }

        audit_entries: list[dict] = []

        try:
            # 1. Verify all approved candidates via customer lookup + name matching
            lookup_results = await self._verify_recipients(approved_candidates, audit_entries)

            # 2. Split into verified vs failed/mismatched
            verified_candidates = []
            failed_candidates = []
            for c, lr in zip(approved_candidates, lookup_results):
                if lr["lookup_status"] == "success":
                    verified_candidates.append(c)
                else:
                    failed_candidates.append(c)

            if failed_candidates:
                logger.warning(
                    f"[ExecutionAgent] {len(failed_candidates)} candidates failed verification"
                )

            # 3. Build execution results for failed lookups (requires_followup)
            candidate_execution_results: list[dict] = [
                {
                    "candidate_id": c["candidate_id"],
                    "execution_status": "requires_followup",
                    "client_reference": None,
                    "provider_reference": None,
                }
                for c in failed_candidates
            ]

            # 4. Execute payouts for verified candidates
            batch_details = None
            if verified_candidates:
                batch_details, exec_results = await self._execute_payouts(
                    state["run_id"], verified_candidates, audit_entries
                )
                candidate_execution_results.extend(exec_results)

            return {
                **state,
                "candidate_lookup_results": lookup_results,
                "candidate_execution_results": candidate_execution_results,
                "batch_details": batch_details,
                "current_step": "execution_complete",
                "audit_entries": audit_entries,
            }
        except Exception as e:
            logger.error(f"[ExecutionAgent] Failed: {e}")
            return {
                **state,
                "candidate_lookup_results": [],
                "candidate_execution_results": [],
                "batch_details": None,
                "error": f"ExecutionAgent failed: {str(e)}",
                "current_step": "execution_failed",
                "audit_entries": [{
                    "agent_type": "execution",
                    "action": "execution_failed",
                    "detail": {"error": str(e)},
                }],
            }

    async def _verify_recipients(
        self, candidates: list[dict], audit_entries: list[dict]
    ) -> list[dict]:
        """Verify each candidate via customer lookup and name matching.

        Returns per-candidate lookup results:
        [{candidate_id, lookup_status, lookup_account_name, lookup_match_score}]
        """
        results = []
        for c in candidates:
            candidate_id = c.get("candidate_id")
            try:
                raw = await self._lookup_client.lookup_customer(
                    institution_code=c["institution_code"],
                    account_number=c["account_number"],
                )

                if raw.get("lookupStatus") != "SUCCESS":
                    results.append({
                        "candidate_id": candidate_id,
                        "lookup_status": "failed",
                        "lookup_account_name": None,
                        "lookup_match_score": None,
                    })
                    audit_entries.append({
                        "agent_type": "execution",
                        "action": "lookup_failed",
                        "detail": {
                            "candidate_id": candidate_id,
                            "raw_status": raw.get("lookupStatus"),
                        },
                        "api_endpoint": "/api/v1/payouts/customer-lookup",
                    })
                    continue

                # Name matching
                lookup_name = raw.get("accountName", "")
                beneficiary_name = c.get("beneficiary_name", "")
                match_score = name_match_score(lookup_name, beneficiary_name)

                if match_score >= NAME_MATCH_THRESHOLD:
                    lookup_status = "success"
                else:
                    lookup_status = "mismatch"
                    logger.warning(
                        f"[ExecutionAgent] Name mismatch for {candidate_id}: "
                        f"'{beneficiary_name}' vs '{lookup_name}' (score={match_score:.3f})"
                    )

                results.append({
                    "candidate_id": candidate_id,
                    "lookup_status": lookup_status,
                    "lookup_account_name": lookup_name,
                    "lookup_match_score": round(match_score, 3),
                })
                audit_entries.append({
                    "agent_type": "execution",
                    "action": f"lookup_{lookup_status}",
                    "detail": {
                        "candidate_id": candidate_id,
                        "lookup_account_name": lookup_name,
                        "match_score": round(match_score, 3),
                    },
                    "api_endpoint": "/api/v1/payouts/customer-lookup",
                })

            except Exception as e:
                logger.warning(f"[ExecutionAgent] Lookup failed for {candidate_id}: {e}")
                results.append({
                    "candidate_id": candidate_id,
                    "lookup_status": "failed",
                    "lookup_account_name": None,
                    "lookup_match_score": None,
                })
                audit_entries.append({
                    "agent_type": "execution",
                    "action": "lookup_failed",
                    "detail": {"candidate_id": candidate_id, "error": str(e)},
                    "api_endpoint": "/api/v1/payouts/customer-lookup",
                })
        return results

    async def _execute_payouts(
        self, run_id: str, candidates: list[dict], audit_entries: list[dict]
    ) -> tuple[dict, list[dict]]:
        """Submit verified candidates as a payout batch.

        Returns (batch_details, per_candidate_execution_results).
        """
        batch_reference = f"FP_{run_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        source_account_id = Settings.INTERSWITCH_SOURCE_ACCOUNT_ID

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

        raw = await self._payout_client.execute_payout(
            batch_reference=batch_reference,
            items=items,
        )

        # Build batch details for orchestrator persistence
        total_amount = sum(c["amount"] for c in candidates)
        batch_details = {
            "batch_reference": batch_reference,
            "currency": candidates[0].get("currency", "NGN"),
            "source_account_id": source_account_id or "",
            "total_amount": total_amount,
            "item_count": len(candidates),
            "submission_status": (raw.get("submissionStatus") or "pending").lower(),
            "accepted_count": raw.get("acceptedCount", 0),
            "rejected_count": raw.get("rejectedCount", 0),
        }

        audit_entries.append({
            "agent_type": "execution",
            "action": "payout_batch_submitted",
            "detail": {
                "batch_reference": batch_reference,
                "item_count": len(candidates),
                "total_amount": total_amount,
                "submission_status": batch_details["submission_status"],
            },
            "api_endpoint": "/api/v1/payouts",
        })

        # Map response items back to candidates for per-candidate execution results
        response_items = {
            item.get("clientReference"): item for item in raw.get("items", [])
        }

        candidate_results: list[dict] = []
        for c in candidates:
            cid = c.get("candidate_id")
            resp_item = response_items.get(cid, {})
            provider_ref = resp_item.get("providerReference")
            item_status = (resp_item.get("status") or "").upper()

            if item_status == "ACCEPTED" or provider_ref:
                exec_status = "pending"
            elif item_status == "REJECTED":
                exec_status = "failed"
            else:
                exec_status = "pending"

            candidate_results.append({
                "candidate_id": cid,
                "execution_status": exec_status,
                "client_reference": cid,
                "provider_reference": provider_ref,
            })

            audit_entries.append({
                "agent_type": "execution",
                "action": f"payout_item_{exec_status}",
                "detail": {
                    "candidate_id": cid,
                    "provider_reference": provider_ref,
                    "item_status": item_status,
                },
                "api_endpoint": "/api/v1/payouts",
            })

        return batch_details, candidate_results

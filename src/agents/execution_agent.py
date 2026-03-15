"""
ExecutionAgent — verifies beneficiaries and executes payouts.

Uses a PayoutGateway abstraction so the transport can be either:
  - LivePayoutGateway  (real Interswitch calls, PAYOUT_MODE=live)
  - SimulatedPayoutGateway (demo-safe, PAYOUT_MODE=simulated)

Flow:
  1. Customer-lookup per candidate → validates account
  2. Name matching (lookup_name vs beneficiary_name)
  3. Create Payout per verified candidate
  4. Poll payout status for final settlement
"""

import logging
from datetime import datetime

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.gateway_factory import get_payout_gateway
from src.infrastructure.external_services.interswitch.payout_gateway import PayoutGateway
from src.utilities.name_match import name_match_score, NAME_MATCH_THRESHOLD

logger = logging.getLogger(__name__)


class ExecutionAgent(BaseAgent):

    def __init__(self, gateway: PayoutGateway | None = None) -> None:
        super().__init__("ExecutionAgent")
        self._gateway = gateway or get_payout_gateway()

    async def run(self, state: AgentState) -> AgentState:
        approved_ids = set(state.get("approved_candidate_ids", []))
        candidates = state.get("scored_candidates", [])

        # Only explicitly approved candidates may execute — never bypass via risk_decision
        approved_candidates = [
            c for c in candidates
            if c.get("candidate_id") in approved_ids
        ]

        # Idempotency: skip candidates already submitted (guards against re-runs).
        # Only terminal/submitted states are skipped; requires_followup is retriable.
        prior_execution_results = state.get("candidate_execution_results", [])
        _SUBMITTED_STATUSES = {"pending", "success", "failed"}
        already_executed = {
            er["candidate_id"]
            for er in prior_execution_results
            if er.get("execution_status") in _SUBMITTED_STATUSES
        }
        if already_executed:
            logger.info(f"[ExecutionAgent] Skipping {len(already_executed)} already-executed candidates")
            approved_candidates = [
                c for c in approved_candidates
                if c.get("candidate_id") not in already_executed
            ]

        logger.info(f"[ExecutionAgent] Processing {len(approved_candidates)} approved candidates")
        await self.emit_progress(f"Processing {len(approved_candidates)} approved candidates")

        if not approved_candidates:
            logger.warning("[ExecutionAgent] No approved candidates to process")
            return {
                **state,
                "candidate_lookup_results": state.get("candidate_lookup_results", []),
                "candidate_execution_results": prior_execution_results,
                "batch_details": state.get("batch_details"),
                "current_step": "execution_complete",
                "audit_entries": [{
                    "agent_type": "execution",
                    "action": "execution_skipped",
                    "detail": {"reason": "no approved candidates"},
                }],
            }

        payout_mode = "simulated" if self._gateway.is_simulated else "live"
        audit_entries: list[dict] = [{
            "agent_type": "execution",
            "action": "payout_mode_selected",
            "detail": {"payout_mode": payout_mode},
        }]
        run_id = state["run_id"]

        try:
            # 1. Verify all approved candidates via customer-lookup + name matching
            await self.emit_progress("Verifying recipient accounts via Interswitch lookup")
            lookup_results = await self._verify_recipients(run_id, approved_candidates, audit_entries)

            # 2. Split into verified vs failed/mismatched
            verified_candidates = []
            failed_candidates = []
            for c, lr in zip(approved_candidates, lookup_results):
                if lr["lookup_status"] == "success":
                    # Carry the transactionReference from lookup → payout
                    c_with_ref = {**c, "_transaction_reference": lr["transaction_reference"]}
                    verified_candidates.append(c_with_ref)
                else:
                    failed_candidates.append(c)

            if failed_candidates:
                logger.warning(
                    f"[ExecutionAgent] {len(failed_candidates)} candidates failed verification"
                )
                await self.emit_progress(f"{len(failed_candidates)} candidates failed verification")

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
                await self.emit_progress(f"Executing payouts for {len(verified_candidates)} verified candidates")
                batch_details, exec_results = await self._execute_payouts(
                    run_id, verified_candidates, audit_entries
                )
                # 5. Poll for final status on pending items
                await self.emit_progress("Polling payout status for pending items")
                exec_results = await self._poll_payout_statuses(exec_results, audit_entries)
                candidate_execution_results.extend(exec_results)

            # Merge with prior execution results from previous runs (idempotency)
            new_ids = {er["candidate_id"] for er in candidate_execution_results}
            preserved_prior = [er for er in prior_execution_results if er["candidate_id"] not in new_ids]
            all_execution_results = preserved_prior + candidate_execution_results

            return {
                **state,
                "candidate_lookup_results": lookup_results,
                "candidate_execution_results": all_execution_results,
                "batch_details": batch_details or state.get("batch_details"),
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
        self, run_id: str, candidates: list[dict], audit_entries: list[dict]
    ) -> list[dict]:
        """Verify each candidate via Interswitch customer-lookup + name matching.

        Generates a unique transactionReference per candidate (format:
        FP_{run_id}_{candidate_id}) which must be reused in the payout call.

        Returns per-candidate lookup results with:
            candidate_id, lookup_status, lookup_account_name,
            lookup_match_score, transaction_reference
        """
        results = []
        total = len(candidates)
        for idx, c in enumerate(candidates, 1):
            candidate_id = c.get("candidate_id")
            txn_ref = f"FP_{run_id}_{candidate_id}"
            await self.emit_progress(f"Verifying account {idx}/{total}: {c.get('account_number', '???')}")
            try:
                raw = await self._gateway.lookup_customer(
                    institution_code=c["institution_code"],
                    account_number=c["account_number"],
                    transaction_reference=txn_ref,
                )

                if raw.get("lookupStatus") != "SUCCESS":
                    results.append({
                        "candidate_id": candidate_id,
                        "lookup_status": "failed",
                        "lookup_account_name": None,
                        "lookup_match_score": None,
                        "transaction_reference": None,
                    })
                    audit_entries.append({
                        "agent_type": "execution",
                        "action": "lookup_failed",
                        "detail": {
                            "candidate_id": candidate_id,
                            "raw_status": raw.get("lookupStatus"),
                            "can_credit": raw.get("canCredit", False),
                        },
                        "api_endpoint": "/payouts/api/v1/payouts/customer-lookup",
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
                    "transaction_reference": txn_ref,
                })
                audit_entries.append({
                    "agent_type": "execution",
                    "action": f"lookup_{lookup_status}",
                    "detail": {
                        "candidate_id": candidate_id,
                        "lookup_account_name": lookup_name,
                        "match_score": round(match_score, 3),
                        "transaction_reference": txn_ref[:30] + "..." if len(txn_ref) > 30 else txn_ref,
                    },
                    "api_endpoint": "/payouts/api/v1/payouts/customer-lookup",
                })

            except Exception as e:
                logger.warning(f"[ExecutionAgent] Lookup failed for {candidate_id}: {e}")
                results.append({
                    "candidate_id": candidate_id,
                    "lookup_status": "failed",
                    "lookup_account_name": None,
                    "lookup_match_score": None,
                    "transaction_reference": None,
                })
                audit_entries.append({
                    "agent_type": "execution",
                    "action": "lookup_failed",
                    "detail": {"candidate_id": candidate_id, "error": str(e)},
                    "api_endpoint": "/payouts/api/v1/payouts/customer-lookup",
                })
        return results

    async def _execute_payouts(
        self, run_id: str, candidates: list[dict], audit_entries: list[dict]
    ) -> tuple[dict, list[dict]]:
        """Execute payouts for verified candidates using per-item POST /payouts/api/v1/payouts.

        Each candidate dict must have _transaction_reference from the
        customer-lookup step.

        Returns (batch_details, per_candidate_execution_results).
        """
        batch_reference = f"FP_{run_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        # Build items for the payout client
        items = [
            {
                "client_reference": c.get("candidate_id", ""),
                "amount": c["amount"],
                "institution_code": c["institution_code"],
                "account_number": c["account_number"],
                "narration": c.get("purpose", "FlowPilot payout"),
                "transaction_reference": c["_transaction_reference"],
            }
            for c in candidates
        ]

        raw = await self._gateway.execute_payout(
            batch_reference=batch_reference,
            items=items,
        )

        # Build batch details for orchestrator persistence
        total_amount = sum(c["amount"] for c in candidates)
        accepted_count = raw.get("acceptedCount", 0)
        rejected_count = raw.get("rejectedCount", 0)
        api_status = (raw.get("submissionStatus") or "pending").lower()

        # Derive submission_status: if API says accepted but counts disagree, mark partial
        if api_status in ("accepted", "pending") and accepted_count < len(candidates) and rejected_count > 0:
            api_status = "partial"

        batch_details = {
            "batch_reference": batch_reference,
            "currency": candidates[0].get("currency", "NGN"),
            "wallet_id": Settings.INTERSWITCH_WALLET_ID,
            "total_amount": total_amount,
            "item_count": len(candidates),
            "submission_status": api_status,
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
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
            "api_endpoint": "/payouts/api/v1/payouts",
        })

        # Map response items back to candidates for per-candidate execution results
        response_items = {
            item.get("clientReference"): item for item in raw.get("items", [])
        }

        candidate_results: list[dict] = []
        for c in candidates:
            cid = c.get("candidate_id")
            resp_item = response_items.get(cid, {})
            provider_ref = resp_item.get("providerReference", "")
            item_status = (resp_item.get("status") or "").upper()

            if item_status == "PENDING":
                exec_status = "pending"
            elif item_status == "FAILED":
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
                "api_endpoint": "/payouts/api/v1/payouts",
            })

        return batch_details, candidate_results

    async def _poll_payout_statuses(
        self, candidate_results: list[dict], audit_entries: list[dict],
        max_polls: int = 2, poll_delay: float = 2.0,
    ) -> list[dict]:
        """Poll final status for pending payout items via GET /payouts/api/v1/payouts/{ref}.

        Makes up to *max_polls* attempts per item, with *poll_delay* seconds
        between attempts.  Only items with a provider_reference and status
        "pending" are polled.
        """
        import asyncio

        pending = [
            r for r in candidate_results
            if r["execution_status"] == "pending" and r.get("provider_reference")
        ]
        if not pending:
            return candidate_results

        logger.info(f"[ExecutionAgent] Polling status for {len(pending)} pending items")

        # Index results by candidate_id for in-place updates
        results_map = {r["candidate_id"]: r for r in candidate_results}

        for attempt in range(max_polls):
            if attempt > 0:
                await asyncio.sleep(poll_delay)

            still_pending = [
                r for r in pending
                if results_map[r["candidate_id"]]["execution_status"] == "pending"
            ]
            if not still_pending:
                break

            for item in still_pending:
                try:
                    status_resp = await self._gateway.requery_payout(
                        transaction_reference=item["provider_reference"],
                    )
                    raw_status = (status_resp.get("status") or "").upper()
                    if raw_status == "SUCCESSFUL":
                        results_map[item["candidate_id"]]["execution_status"] = "success"
                    elif raw_status == "FAILED":
                        results_map[item["candidate_id"]]["execution_status"] = "failed"
                    # "PROCESSING" = still pending, will retry on next poll

                    audit_entries.append({
                        "agent_type": "execution",
                        "action": f"payout_poll_{results_map[item['candidate_id']]['execution_status']}",
                        "detail": {
                            "candidate_id": item["candidate_id"],
                            "provider_reference": item["provider_reference"],
                            "poll_attempt": attempt + 1,
                            "raw_status": raw_status,
                        },
                        "api_endpoint": f"/payouts/api/v1/payouts/{item['provider_reference'][:30]}",
                    })
                except Exception as e:
                    logger.warning(
                        f"[ExecutionAgent] Status check failed for {item['provider_reference']}: {e}"
                    )

        return list(results_map.values())

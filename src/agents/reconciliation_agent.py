import logging
from datetime import date, datetime, time, timedelta

import httpx

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.infrastructure.external_services.interswitch.transaction_search import TransactionSearchClient
from src.config.settings import Settings

logger = logging.getLogger(__name__)

_QUICK_SEARCH_ENDPOINT = "/transaction-search/quick-search"
_REFERENCE_SEARCH_ENDPOINT = "/transaction-search/reference-search"


_VALID_TXN_STATUSES = {"SUCCESS", "PENDING", "FAILED", "REVERSED"}


class ReconciliationAgent(BaseAgent):

    def __init__(self) -> None:
        super().__init__("ReconciliationAgent")
        self._search_client = TransactionSearchClient()

    def _resolve_window(self, state: AgentState) -> tuple[datetime, datetime]:
        raw_from = state.get("date_from")
        raw_to = state.get("date_to")

        if raw_from:
            start_date = datetime.combine(date.fromisoformat(raw_from), time.min)
        else:
            start_date = datetime.utcnow() - timedelta(days=1)

        if raw_to:
            end_date = datetime.combine(date.fromisoformat(raw_to), time.max)
        else:
            end_date = datetime.utcnow()

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        return start_date, end_date

    def _format_exception(self, error: Exception) -> str:
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            body = error.response.text.strip()
            body = body[:300] if body else ""
            detail = f"HTTP {status} from {error.request.method} {error.request.url}"
            return f"{detail}: {body}" if body else detail

        message = str(error).strip()
        if message:
            return message

        return type(error).__name__

    def _build_simulated_result(
        self,
        state: AgentState,
        merchant_id: str,
        start_date: datetime,
        end_date: datetime,
    ) -> AgentState:
        audit_entries: list[dict] = [{
            "agent_type": "reconciliation",
            "action": "reconciliation_simulated",
            "detail": {
                "merchant_id": merchant_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "reason": "demo_mode",
                "total_fetched": 0,
            },
        }]

        return {
            **state,
            "transactions": [],
            "reconciled_ledger": self._build_ledger([]),
            "unresolved_references": [],
            "resolved_references": [],
            "current_step": "reconciliation_complete",
            "audit_entries": audit_entries,
        }

    async def run(self, state: AgentState) -> AgentState:
        merchant_id = state.get("merchant_id", Settings.INTERSWITCH_MERCHANT_ID)
        logger.info(f"[ReconciliationAgent] Starting reconciliation for merchant {merchant_id}")

        try:
            start_date, end_date = self._resolve_window(state)
            await self.emit_progress(f"Reconciling transactions for merchant {merchant_id}")

            if Settings.is_payout_simulated():
                logger.info("[ReconciliationAgent] Demo mode enabled - skipping Interswitch transaction search")
                await self.emit_progress("Demo mode — using simulated transaction data")
                return self._build_simulated_result(state, merchant_id, start_date, end_date)

            await self.emit_progress(f"Searching transactions from {start_date.date()} to {end_date.date()}")
            search_result = await self._search_client.quick_search(
                merchant_id=merchant_id,
                start_date=start_date,
                end_date=end_date,
            )

            transactions = search_result.get("transactions", [])
            logger.info(f"[ReconciliationAgent] Fetched {len(transactions)} transactions")
            await self.emit_progress(f"Fetched {len(transactions)} transactions from Interswitch")

            audit_entries: list[dict] = [{
                "agent_type": "reconciliation",
                "action": "quick_search_complete",
                "detail": {
                    "merchant_id": merchant_id,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "total_fetched": len(transactions),
                },
                "api_endpoint": _QUICK_SEARCH_ENDPOINT,
            }]

            ledger = self._build_ledger(transactions)

            unresolved = [
                t["transactionReference"]
                for t in transactions
                if t.get("status") == "PENDING"
            ]

            # Build index for in-place updates when reference_search resolves a txn
            txn_by_ref = {t["transactionReference"]: t for t in transactions}
            resolved_refs: list[dict] = []

            for ref in unresolved[:10]:
                try:
                    await self.emit_progress(f"Resolving pending reference {ref[:20]}…")
                    detail = await self._search_client.reference_search(
                        transaction_reference=ref,
                        merchant_id=merchant_id,
                    )
                    new_status = detail.get("status")
                    if new_status and new_status != "PENDING" and new_status in _VALID_TXN_STATUSES:
                        # Update the transaction dict with resolved data
                        if ref in txn_by_ref:
                            txn_by_ref[ref]["status"] = new_status
                            if detail.get("processorResponseCode"):
                                txn_by_ref[ref]["processorResponseCode"] = detail["processorResponseCode"]
                            if detail.get("processorResponseMessage"):
                                txn_by_ref[ref]["processorResponseMessage"] = detail["processorResponseMessage"]
                            if detail.get("settlementDate"):
                                txn_by_ref[ref]["settlementDate"] = detail["settlementDate"]
                        resolved_refs.append({
                            "transaction_reference": ref,
                            "new_status": new_status,
                            "processor_response_code": detail.get("processorResponseCode"),
                            "processor_response_message": detail.get("processorResponseMessage"),
                            "settlement_date": detail.get("settlementDate"),
                        })
                        unresolved.remove(ref)

                    audit_entries.append({
                        "agent_type": "reconciliation",
                        "action": "reference_search_complete",
                        "detail": {
                            "transaction_reference": ref,
                            "resolved": new_status != "PENDING" if new_status else False,
                            "new_status": new_status,
                        },
                        "api_endpoint": _REFERENCE_SEARCH_ENDPOINT,
                    })
                except Exception as e:
                    logger.warning(f"[ReconciliationAgent] Reference search failed for {ref}: {e}")
                    audit_entries.append({
                        "agent_type": "reconciliation",
                        "action": "reference_search_failed",
                        "detail": {"transaction_reference": ref, "error": str(e)},
                        "api_endpoint": _REFERENCE_SEARCH_ENDPOINT,
                    })

            # Rebuild ledger after resolutions
            ledger = self._build_ledger(transactions)

            logger.info(f"[ReconciliationAgent] Ledger: {ledger}, unresolved: {len(unresolved)}, resolved: {len(resolved_refs)}")
            await self.emit_progress(
                f"Reconciliation complete — {len(transactions)} txns, "
                f"{len(resolved_refs)} resolved, {len(unresolved)} unresolved"
            )

            audit_entries.append({
                "agent_type": "reconciliation",
                "action": "reconciliation_complete",
                "detail": {
                    "total_transactions": len(transactions),
                    "unresolved_count": len(unresolved),
                    "resolved_count": len(resolved_refs),
                    "ledger_summary": ledger,
                },
            })

            return {
                **state,
                "transactions": transactions,
                "reconciled_ledger": ledger,
                "unresolved_references": unresolved,
                "resolved_references": resolved_refs,
                "current_step": "reconciliation_complete",
                "audit_entries": audit_entries,
            }
        except Exception as e:
            error_message = self._format_exception(e)
            logger.error(f"[ReconciliationAgent] Failed: {error_message}", exc_info=True)
            return {
                **state,
                "error": f"ReconciliationAgent failed: {error_message}",
                "current_step": "reconciliation_failed",
                "audit_entries": [{
                    "agent_type": "reconciliation",
                    "action": "reconciliation_failed",
                    "detail": {"error": error_message},
                }],
            }

    def _build_ledger(self, transactions: list[dict]) -> dict:
        ledger = {
            "total_inflow": 0.0,
            "total_outflow": 0.0,
            "pending_amount": 0.0,
            "failed_amount": 0.0,
            "success_count": 0,
            "pending_count": 0,
            "failed_count": 0,
            "reversed_count": 0,
        }

        for t in transactions:
            amount = t.get("amount", 0.0)
            status = t.get("status", "")

            if status == "SUCCESS":
                ledger["total_inflow"] += amount
                ledger["success_count"] += 1
            elif status == "PENDING":
                ledger["pending_amount"] += amount
                ledger["pending_count"] += 1
            elif status == "FAILED":
                ledger["failed_amount"] += amount
                ledger["failed_count"] += 1
            elif status == "REVERSED":
                ledger["reversed_count"] += 1

        return ledger

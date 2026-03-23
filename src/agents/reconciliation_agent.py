import logging
from datetime import datetime, timedelta

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.infrastructure.external_services.interswitch.transaction_search import TransactionSearchClient
from src.config.settings import Settings

logger = logging.getLogger(__name__)


class ReconciliationAgent(BaseAgent):

    def __init__(self) -> None:
        super().__init__("ReconciliationAgent")
        self._search_client = TransactionSearchClient()

    async def run(self, state: AgentState) -> AgentState:
        merchant_id = state.get("merchant_id", Settings.INTERSWITCH_MERCHANT_ID)
        logger.info(f"[ReconciliationAgent] Starting reconciliation for merchant {merchant_id}")

        try:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=1)

            search_result = await self._search_client.quick_search(
                merchant_id=merchant_id,
                start_date=start_date,
                end_date=end_date,
            )

            transactions = search_result.get("transactions", [])
            logger.info(f"[ReconciliationAgent] Fetched {len(transactions)} transactions")

            ledger = self._build_ledger(transactions)

            unresolved = [
                t["transactionReference"]
                for t in transactions
                if t.get("status") == "PENDING"
            ]

            resolved_details = []
            for ref in unresolved[:10]:
                try:
                    detail = await self._search_client.reference_search(
                        transaction_reference=ref,
                        merchant_id=merchant_id,
                    )
                    resolved_details.append(detail)
                    if detail.get("status") == "SUCCESS":
                        unresolved.remove(ref)
                except Exception as e:
                    logger.warning(f"[ReconciliationAgent] Reference search failed for {ref}: {e}")

            logger.info(f"[ReconciliationAgent] Ledger: {ledger}, unresolved: {len(unresolved)}")

            return {
                **state,
                "transactions": transactions,
                "reconciled_ledger": ledger,
                "unresolved_references": unresolved,
                "current_step": "reconciliation_complete",
                "audit_entries": [{
                    "agent_type": "reconciliation",
                    "action": "reconciliation_complete",
                    "detail": {
                        "total_transactions": len(transactions),
                        "unresolved_count": len(unresolved),
                        "ledger_summary": ledger,
                    },
                    "created_at": datetime.utcnow().isoformat(),
                }],
            }
        except Exception as e:
            logger.error(f"[ReconciliationAgent] Failed: {e}")
            return {
                **state,
                "error": f"ReconciliationAgent failed: {str(e)}",
                "current_step": "reconciliation_failed",
                "audit_entries": [{
                    "agent_type": "reconciliation",
                    "action": "reconciliation_failed",
                    "detail": {"error": str(e)},
                    "created_at": datetime.utcnow().isoformat(),
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

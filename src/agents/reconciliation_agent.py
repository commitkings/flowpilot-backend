import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry
from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.transaction_search import (
    TransactionSearchClient,
)

logger = logging.getLogger(__name__)

_VALID_TXN_STATUSES = {"SUCCESS", "PENDING", "FAILED", "REVERSED"}

RECONCILIATION_SYSTEM_PROMPT = """You are a financial reconciliation agent for FlowPilot.

Your job: pull transaction data from Interswitch, reconcile it, detect anomalies, and produce a structured reconciliation result.

## Your workflow:
1. Use `search_transactions` to fetch transactions for the given date range and merchant
2. Analyze the results — look at status distribution, amounts, patterns
3. Use `search_by_reference` to investigate any PENDING transactions (up to 10)
4. Use `compute_ledger_summary` to get aggregated financial totals
5. Use `detect_anomalies` to check for unusual patterns in the transaction data
6. Use `flag_unresolved` to mark any transactions that remain unresolved after investigation

## Anomaly patterns to look for:
- Unusually large transactions relative to the average
- Clusters of failures at specific times
- Duplicate transaction references
- Transactions with mismatched amounts or statuses
- Suspicious patterns in beneficiary accounts (many payouts to same account)

## Final answer format (JSON):
{
  "reconciliation_summary": {
    "total_transactions": 0,
    "status_breakdown": {"SUCCESS": 0, "PENDING": 0, "FAILED": 0, "REVERSED": 0},
    "total_inflow": 0.0,
    "total_outflow": 0.0,
    "anomalies_detected": [],
    "unresolved_count": 0,
    "resolved_count": 0,
    "data_quality_notes": "Any observations about data quality or completeness"
  }
}
"""


def _build_reconciliation_tools(state: AgentState) -> tuple[list[Tool], dict[str, Any]]:
    search_client = TransactionSearchClient()

    shared_data: dict[str, Any] = {
        "transactions": [],
        "resolved_refs": [],
        "unresolved_refs": [],
    }

    def _resolve_window() -> tuple[datetime, datetime]:
        raw_from = state.get("date_from")
        raw_to = state.get("date_to")
        start_date = (
            datetime.combine(date.fromisoformat(raw_from), time.min)
            if raw_from
            else datetime.utcnow() - timedelta(days=1)
        )
        end_date = (
            datetime.combine(date.fromisoformat(raw_to), time.max)
            if raw_to
            else datetime.utcnow()
        )
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date

    async def search_transactions(
        statuses: str = "SUCCESS,PENDING,FAILED",
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        merchant_id = state.get("merchant_id", Settings.INTERSWITCH_MERCHANT_ID)
        start_date, end_date = _resolve_window()
        status_list = [s.strip() for s in statuses.split(",")]

        if Settings.is_payout_simulated():
            shared_data["transactions"] = []
            return {
                "mode": "simulated",
                "transactions": [],
                "total_count": 0,
                "note": "Simulated mode — no real transactions available",
                "merchant_id": merchant_id,
                "date_range": f"{start_date.date()} to {end_date.date()}",
            }

        result = await search_client.quick_search(
            merchant_id=merchant_id,
            start_date=start_date,
            end_date=end_date,
            statuses=status_list,
            page=page,
            page_size=page_size,
        )
        txns = result.get("transactions", [])
        shared_data["transactions"] = txns
        return {
            "total_count": len(txns),
            "transactions": txns[:20],
            "truncated": len(txns) > 20,
            "full_count": len(txns),
            "merchant_id": merchant_id,
            "date_range": f"{start_date.date()} to {end_date.date()}",
            "statuses_requested": status_list,
        }

    async def search_by_reference(transaction_reference: str) -> dict[str, Any]:
        merchant_id = state.get("merchant_id", Settings.INTERSWITCH_MERCHANT_ID)

        if Settings.is_payout_simulated():
            return {
                "mode": "simulated",
                "reference": transaction_reference,
                "status": "SUCCESS",
                "note": "Simulated lookup",
            }

        detail = await search_client.reference_search(
            transaction_reference=transaction_reference,
            merchant_id=merchant_id,
        )
        new_status = detail.get("status")

        if new_status and new_status != "PENDING" and new_status in _VALID_TXN_STATUSES:
            for txn in shared_data["transactions"]:
                if txn.get("transactionReference") == transaction_reference:
                    txn["status"] = new_status
                    if detail.get("processorResponseCode"):
                        txn["processorResponseCode"] = detail["processorResponseCode"]
                    break
            shared_data["resolved_refs"].append(
                {
                    "transaction_reference": transaction_reference,
                    "new_status": new_status,
                    "processor_response_code": detail.get("processorResponseCode"),
                    "processor_response_message": detail.get(
                        "processorResponseMessage"
                    ),
                }
            )
            if transaction_reference in shared_data["unresolved_refs"]:
                shared_data["unresolved_refs"].remove(transaction_reference)

        return {
            "reference": transaction_reference,
            "status": new_status,
            "resolved": new_status != "PENDING" if new_status else False,
            "detail": {
                "processor_response_code": detail.get("processorResponseCode"),
                "processor_response_message": detail.get("processorResponseMessage"),
                "settlement_date": detail.get("settlementDate"),
            },
        }

    async def compute_ledger_summary() -> dict[str, Any]:
        txns = shared_data["transactions"]
        ledger = {
            "total_inflow": 0.0,
            "total_outflow": 0.0,
            "pending_amount": 0.0,
            "failed_amount": 0.0,
            "success_count": 0,
            "pending_count": 0,
            "failed_count": 0,
            "reversed_count": 0,
            "total_transactions": len(txns),
        }

        for t in txns:
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

    async def detect_anomalies(threshold_multiplier: float = 3.0) -> dict[str, Any]:
        txns = shared_data["transactions"]
        if not txns:
            return {"anomalies": [], "note": "No transactions to analyze"}

        amounts = [t.get("amount", 0.0) for t in txns if t.get("amount")]
        if not amounts:
            return {"anomalies": [], "note": "No amounts found in transactions"}

        avg_amount = sum(amounts) / len(amounts)
        max_normal = avg_amount * threshold_multiplier

        anomalies = []
        flagged_refs: dict[str, list[dict]] = {}

        for t in txns:
            amount = t.get("amount", 0.0)
            if amount > max_normal:
                ref = t.get("transactionReference", "")
                anomaly = {
                    "type": "large_amount",
                    "reference": ref,
                    "amount": amount,
                    "average": round(avg_amount, 2),
                    "multiplier": round(amount / avg_amount, 1)
                    if avg_amount > 0
                    else 0,
                }
                anomalies.append(anomaly)
                flagged_refs.setdefault(ref, []).append(anomaly)

        ref_counts: dict[str, int] = {}
        for t in txns:
            ref = t.get("transactionReference", "")
            ref_counts[ref] = ref_counts.get(ref, 0) + 1
        for ref, count in ref_counts.items():
            if count > 1:
                anomaly = {
                    "type": "duplicate_reference",
                    "reference": ref,
                    "occurrence_count": count,
                }
                anomalies.append(anomaly)
                flagged_refs.setdefault(ref, []).append(anomaly)

        account_counts: dict[str, int] = {}
        for t in txns:
            acct = t.get("accountNumber", t.get("recipientAccount", ""))
            if acct:
                account_counts[acct] = account_counts.get(acct, 0) + 1
        for acct, count in account_counts.items():
            if count > 5:
                anomalies.append(
                    {
                        "type": "high_frequency_account",
                        "account": acct,
                        "transaction_count": count,
                    }
                )

        failed_txns = [t for t in txns if t.get("status") == "FAILED"]
        failure_rate = len(failed_txns) / len(txns) if txns else 0
        if failure_rate > 0.3:
            anomalies.append(
                {
                    "type": "high_failure_rate",
                    "failure_rate": round(failure_rate, 3),
                    "failed_count": len(failed_txns),
                    "total_count": len(txns),
                }
            )

        for t in txns:
            ref = t.get("transactionReference", "")
            ref_anomalies = flagged_refs.get(ref, [])
            if ref_anomalies:
                t["isAnomaly"] = True
                t["anomalies"] = ref_anomalies
            else:
                t["isAnomaly"] = False
                t["anomalies"] = []

        return {
            "anomalies": anomalies,
            "stats": {
                "average_amount": round(avg_amount, 2),
                "threshold": round(max_normal, 2),
                "total_analyzed": len(txns),
                "failure_rate": round(failure_rate, 3),
                "flagged_transaction_count": sum(1 for t in txns if t.get("isAnomaly")),
            },
        }

    async def flag_unresolved() -> dict[str, Any]:
        txns = shared_data["transactions"]
        still_pending = [
            t.get("transactionReference", "")
            for t in txns
            if t.get("status") == "PENDING"
        ]
        shared_data["unresolved_refs"] = still_pending
        return {
            "unresolved_references": still_pending,
            "count": len(still_pending),
        }

    tools = [
        Tool(
            name="search_transactions",
            description="Search Interswitch transactions by date range and status. Returns transaction data for the merchant.",
            parameters=[
                ToolParam(
                    name="statuses",
                    param_type=ToolParamType.STRING,
                    description="Comma-separated statuses to filter: SUCCESS,PENDING,FAILED",
                    required=False,
                    default="SUCCESS,PENDING,FAILED",
                ),
                ToolParam(
                    name="page",
                    param_type=ToolParamType.INTEGER,
                    description="Page number for pagination",
                    required=False,
                    default=1,
                ),
                ToolParam(
                    name="page_size",
                    param_type=ToolParamType.INTEGER,
                    description="Number of results per page",
                    required=False,
                    default=100,
                ),
            ],
            execute=search_transactions,
        ),
        Tool(
            name="search_by_reference",
            description="Look up a single transaction by its reference to resolve pending status or get detailed info.",
            parameters=[
                ToolParam(
                    name="transaction_reference",
                    param_type=ToolParamType.STRING,
                    description="The transaction reference to look up",
                ),
            ],
            execute=search_by_reference,
        ),
        Tool(
            name="compute_ledger_summary",
            description="Compute aggregated financial totals (inflow, outflow, pending, failed) from the fetched transactions.",
            parameters=[],
            execute=compute_ledger_summary,
        ),
        Tool(
            name="detect_anomalies",
            description="Analyze transactions for anomalies: large amounts, duplicates, high-frequency accounts, high failure rates.",
            parameters=[
                ToolParam(
                    name="threshold_multiplier",
                    param_type=ToolParamType.NUMBER,
                    description="Multiplier of average amount to flag as anomalous (default 3.0)",
                    required=False,
                    default=3.0,
                ),
            ],
            execute=detect_anomalies,
        ),
        Tool(
            name="flag_unresolved",
            description="Identify and flag all transactions that remain in PENDING status after investigation.",
            parameters=[],
            execute=flag_unresolved,
        ),
    ]

    return tools, shared_data


def _format_exception(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        body = error.response.text.strip()[:300]
        detail = f"HTTP {status} from {error.request.method} {error.request.url}"
        return f"{detail}: {body}" if body else detail
    message = str(error).strip()
    return message if message else type(error).__name__


class ReconciliationAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("ReconciliationAgent")

    async def run(self, state: AgentState, db_session=None) -> AgentState:
        merchant_id = state.get("merchant_id", Settings.INTERSWITCH_MERCHANT_ID)
        logger.info(
            f"[ReconciliationAgent] Starting reconciliation for merchant {merchant_id}"
        )

        tools, shared_data = _build_reconciliation_tools(state)
        self.registry = ToolRegistry()
        for tool in tools:
            self.registry.register(tool)

        user_prompt = f"""Reconcile transactions for merchant {merchant_id}.

Date range: {state.get("date_from", "yesterday")} to {state.get("date_to", "today")}
Objective context: {state.get("objective", "Standard reconciliation")}

Steps:
1. Search for all transactions in the date range
2. Analyze the results and compute the ledger summary
3. Investigate any PENDING transactions (use search_by_reference for up to 10)
4. Run anomaly detection
5. Flag any remaining unresolved transactions
6. Produce your final reconciliation summary as JSON"""

        try:
            await self.emit_progress(
                f"Reconciling transactions for merchant {merchant_id}"
            )

            response = await self.reason_and_act_json(
                system_prompt=RECONCILIATION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

            try:
                summary = json.loads(response)
            except json.JSONDecodeError:
                summary = {"reconciliation_summary": {"raw_response": response}}

            transactions = shared_data["transactions"]
            resolved_refs = shared_data["resolved_refs"]
            unresolved_refs = shared_data["unresolved_refs"]

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

            await self.emit_progress(
                f"Reconciliation complete — {len(transactions)} txns, "
                f"{len(resolved_refs)} resolved, {len(unresolved_refs)} unresolved"
            )

            audit_entries: list[dict] = [
                {
                    "agent_type": "reconciliation",
                    "action": "reconciliation_complete",
                    "detail": {
                        "total_transactions": len(transactions),
                        "unresolved_count": len(unresolved_refs),
                        "resolved_count": len(resolved_refs),
                        "ledger_summary": ledger,
                        "ai_summary": summary.get("reconciliation_summary", {}),
                    },
                    "created_at": datetime.utcnow().isoformat(),
                }
            ]

            return {
                **state,
                "transactions": transactions,
                "reconciled_ledger": ledger,
                "unresolved_references": unresolved_refs,
                "resolved_references": resolved_refs,
                "current_step": "reconciliation_complete",
                "audit_entries": audit_entries,
            }
        except Exception as e:
            error_message = _format_exception(e)
            logger.error(
                f"[ReconciliationAgent] Failed: {error_message}", exc_info=True
            )
            return {
                **state,
                "error": f"ReconciliationAgent failed: {error_message}",
                "current_step": "reconciliation_failed",
                "audit_entries": [
                    {
                        "agent_type": "reconciliation",
                        "action": "reconciliation_failed",
                        "detail": {"error": error_message},
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }

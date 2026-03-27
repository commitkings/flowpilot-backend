import json
import logging
import statistics
from collections import defaultdict
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

Your job: pull transaction data from Interswitch, reconcile it, detect anomalies, analyze patterns, and produce an intelligent reconciliation result with actionable insights.

## Your workflow:
1. Use `search_transactions` to fetch transactions for the given date range and merchant
2. Analyze the results — look at status distribution, amounts, patterns
3. Use `search_by_reference` to investigate any PENDING transactions (up to 10)
4. Use `compute_ledger_summary` to get aggregated financial totals
5. Use `detect_anomalies` to check for unusual patterns in the transaction data
6. Use `analyze_patterns` to identify recurring payments, seasonal trends, and time-based patterns
7. Use `identify_gaps` to find missing expected transactions based on historical data
8. Use `explain_findings` to generate human-readable explanations for all detected issues
9. Use `flag_unresolved` to mark any transactions that remain unresolved after investigation

## Anomaly patterns to look for:
- Unusually large transactions relative to the average
- Clusters of failures at specific times (potential outages)
- Duplicate transaction references
- Transactions with mismatched amounts or statuses
- Suspicious patterns in beneficiary accounts (many payouts to same account)
- Day-of-week or time-of-day anomalies
- Settlement delays beyond normal windows
- Currency mismatches or unexpected channels

## Pattern analysis:
- Recurring payments (same amount, same account, regular intervals)
- Seasonal trends (higher volumes on certain days/weeks)
- Time clustering (unusual activity bursts)
- Account velocity (rapid transactions to/from same account)

## Final answer format (JSON):
{
  "reconciliation_summary": {
    "total_transactions": 0,
    "status_breakdown": {"SUCCESS": 0, "PENDING": 0, "FAILED": 0, "REVERSED": 0},
    "total_inflow": 0.0,
    "total_outflow": 0.0,
    "anomalies_detected": [],
    "patterns_identified": [],
    "gaps_found": [],
    "insights": [],
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

        if Settings.is_reconciliation_simulated():
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

        if Settings.is_reconciliation_simulated():
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

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: analyze_patterns — detect recurring payments, time patterns
    # ────────────────────────────────────────────────────────────────────────
    async def analyze_patterns() -> dict[str, Any]:
        """Analyze transaction patterns: recurring payments, time clustering, day-of-week patterns."""
        txns = shared_data["transactions"]
        if not txns:
            return {"patterns": [], "note": "No transactions to analyze"}

        patterns: list[dict[str, Any]] = []

        # 1. Recurring payment detection (same account + similar amounts at regular intervals)
        account_txns: dict[str, list[dict]] = defaultdict(list)
        for t in txns:
            acct = t.get("accountNumber", t.get("recipientAccount", ""))
            if acct:
                account_txns[acct].append(t)

        for acct, acct_list in account_txns.items():
            if len(acct_list) >= 3:
                # Check for similar amounts
                amounts = [t.get("amount", 0) for t in acct_list]
                if amounts and len(amounts) >= 3:
                    try:
                        avg_amt = statistics.mean(amounts)
                        stdev_amt = statistics.stdev(amounts) if len(amounts) > 1 else 0
                        # Low variance = recurring
                        cv = (stdev_amt / avg_amt) if avg_amt > 0 else 999
                        if cv < 0.1:  # Less than 10% variation
                            patterns.append(
                                {
                                    "type": "recurring_payment",
                                    "account": acct[-4:].rjust(len(acct), "*"),  # Mask
                                    "count": len(acct_list),
                                    "typical_amount": round(avg_amt, 2),
                                    "variation_pct": round(cv * 100, 1),
                                    "insight": f"Likely recurring payment (salary/subscription) - {len(acct_list)} payments averaging ₦{avg_amt:,.2f}",
                                }
                            )
                    except (statistics.StatisticsError, ZeroDivisionError):
                        pass

        # 2. Day-of-week patterns
        day_counts: dict[str, int] = defaultdict(int)
        day_amounts: dict[str, float] = defaultdict(float)
        for t in txns:
            ts = t.get("transactionTimestamp") or t.get("createdAt")
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        dt = ts
                    day_name = dt.strftime("%A")
                    day_counts[day_name] += 1
                    day_amounts[day_name] += t.get("amount", 0)
                except (ValueError, TypeError):
                    pass

        if day_counts:
            total_txns = sum(day_counts.values())
            avg_per_day = total_txns / len(day_counts) if day_counts else 0
            for day, count in day_counts.items():
                if count > avg_per_day * 1.5:  # 50% above average
                    patterns.append(
                        {
                            "type": "day_of_week_spike",
                            "day": day,
                            "transaction_count": count,
                            "total_amount": round(day_amounts[day], 2),
                            "above_average_by": f"{round((count / avg_per_day - 1) * 100)}%",
                            "insight": f"Higher than normal activity on {day}s - could indicate scheduled batch processing",
                        }
                    )

        # 3. Hour-of-day clustering (unusual hours)
        hour_counts: dict[int, int] = defaultdict(int)
        for t in txns:
            ts = t.get("transactionTimestamp") or t.get("createdAt")
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        dt = ts
                    hour_counts[dt.hour] += 1
                except (ValueError, TypeError):
                    pass

        off_hours = sum(hour_counts.get(h, 0) for h in range(0, 6))  # Midnight to 6am
        total_with_time = sum(hour_counts.values())
        if total_with_time > 0 and off_hours > total_with_time * 0.1:
            patterns.append(
                {
                    "type": "off_hours_activity",
                    "off_hours_count": off_hours,
                    "off_hours_pct": round(off_hours / total_with_time * 100, 1),
                    "insight": f"{off_hours} transactions ({round(off_hours / total_with_time * 100, 1)}%) occurred between midnight and 6am - review for legitimacy",
                }
            )

        # 4. Settlement delay pattern
        settlement_delays: list[int] = []
        for t in txns:
            ts = t.get("transactionTimestamp")
            settle = t.get("settlementDate")
            if ts and settle:
                try:
                    if isinstance(ts, str):
                        txn_date = datetime.fromisoformat(
                            ts.replace("Z", "+00:00")
                        ).date()
                    else:
                        txn_date = ts.date() if hasattr(ts, "date") else ts

                    if isinstance(settle, str):
                        settle_date = datetime.fromisoformat(settle).date()
                    else:
                        settle_date = settle

                    delay = (settle_date - txn_date).days
                    if delay >= 0:
                        settlement_delays.append(delay)
                except (ValueError, TypeError, AttributeError):
                    pass

        if settlement_delays:
            avg_delay = statistics.mean(settlement_delays)
            max_delay = max(settlement_delays)
            long_delays = [d for d in settlement_delays if d > 3]
            if long_delays:
                patterns.append(
                    {
                        "type": "settlement_delays",
                        "average_delay_days": round(avg_delay, 1),
                        "max_delay_days": max_delay,
                        "delayed_count": len(long_delays),
                        "insight": f"{len(long_delays)} transactions took more than 3 days to settle (max: {max_delay} days) - may indicate processing issues",
                    }
                )

        shared_data["patterns"] = patterns
        return {
            "patterns": patterns,
            "total_patterns_found": len(patterns),
        }

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: identify_gaps — find missing expected transactions
    # ────────────────────────────────────────────────────────────────────────
    async def identify_gaps(expected_count: int = 0) -> dict[str, Any]:
        """Identify gaps: missing expected transactions, irregular intervals in recurring payments."""
        txns = shared_data["transactions"]
        gaps: list[dict[str, Any]] = []

        # 1. Check against expected count if provided
        actual_count = len(txns)
        if expected_count > 0 and actual_count < expected_count:
            gap_count = expected_count - actual_count
            gaps.append(
                {
                    "type": "count_shortfall",
                    "expected": expected_count,
                    "actual": actual_count,
                    "missing": gap_count,
                    "severity": "high"
                    if gap_count > expected_count * 0.2
                    else "medium",
                    "insight": f"Expected {expected_count} transactions but only found {actual_count} - {gap_count} transactions may be missing or delayed",
                }
            )

        # 2. Check for date gaps in the date range
        start_date, end_date = _resolve_window()
        date_range_days = (end_date - start_date).days + 1

        dates_with_txns: set[date] = set()
        for t in txns:
            ts = t.get("transactionTimestamp") or t.get("createdAt")
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        dt = ts
                    dates_with_txns.add(dt.date())
                except (ValueError, TypeError):
                    pass

        # If we have transactions on some days, check for gaps
        if len(dates_with_txns) >= 3 and date_range_days <= 31:
            all_dates = set()
            current = start_date.date() if hasattr(start_date, "date") else start_date
            end = end_date.date() if hasattr(end_date, "date") else end_date
            while current <= end:
                # Skip weekends for business transactions
                if current.weekday() < 5:  # Monday=0, Friday=4
                    all_dates.add(current)
                current += timedelta(days=1)

            missing_dates = all_dates - dates_with_txns
            if missing_dates and len(missing_dates) <= 10:
                gaps.append(
                    {
                        "type": "date_gaps",
                        "missing_dates": sorted([d.isoformat() for d in missing_dates])[
                            :5
                        ],
                        "missing_count": len(missing_dates),
                        "severity": "low",
                        "insight": f"No transactions on {len(missing_dates)} business day(s) - this may be normal or indicate missing data",
                    }
                )

        # 3. Check for amount discrepancies in SUCCESS vs expected
        success_txns = [t for t in txns if t.get("status") == "SUCCESS"]
        failed_txns = [t for t in txns if t.get("status") == "FAILED"]

        if failed_txns:
            failed_total = sum(t.get("amount", 0) for t in failed_txns)
            gaps.append(
                {
                    "type": "failed_amount_gap",
                    "failed_transaction_count": len(failed_txns),
                    "failed_total_amount": round(failed_total, 2),
                    "severity": "medium",
                    "insight": f"₦{failed_total:,.2f} in {len(failed_txns)} failed transaction(s) - these may need to be retried or investigated",
                }
            )

        # 4. Check for pending that exceed normal settlement window
        pending_txns = [t for t in txns if t.get("status") == "PENDING"]
        old_pending = []
        for t in pending_txns:
            ts = t.get("transactionTimestamp") or t.get("createdAt")
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        dt = ts
                    age_days = (datetime.utcnow() - dt.replace(tzinfo=None)).days
                    if age_days > 2:  # Older than 2 days
                        old_pending.append(
                            {
                                "reference": t.get("transactionReference", ""),
                                "amount": t.get("amount", 0),
                                "age_days": age_days,
                            }
                        )
                except (ValueError, TypeError):
                    pass

        if old_pending:
            total_stuck = sum(p["amount"] for p in old_pending)
            gaps.append(
                {
                    "type": "stale_pending",
                    "count": len(old_pending),
                    "total_amount": round(total_stuck, 2),
                    "oldest_days": max(p["age_days"] for p in old_pending),
                    "references": [p["reference"] for p in old_pending[:5]],
                    "severity": "high",
                    "insight": f"{len(old_pending)} pending transaction(s) totaling ₦{total_stuck:,.2f} are older than 2 days - these likely need manual intervention",
                }
            )

        shared_data["gaps"] = gaps
        return {
            "gaps": gaps,
            "total_gaps_found": len(gaps),
        }

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: explain_findings — LLM-consumable summary of issues
    # ────────────────────────────────────────────────────────────────────────
    async def explain_findings() -> dict[str, Any]:
        """Generate human-readable explanations and actionable insights from all detected issues."""
        txns = shared_data["transactions"]
        patterns = shared_data.get("patterns", [])
        gaps = shared_data.get("gaps", [])
        resolved = shared_data.get("resolved_refs", [])
        unresolved = shared_data.get("unresolved_refs", [])

        insights: list[dict[str, Any]] = []

        # Overall health assessment
        total = len(txns)
        success_count = sum(1 for t in txns if t.get("status") == "SUCCESS")
        failed_count = sum(1 for t in txns if t.get("status") == "FAILED")
        pending_count = sum(1 for t in txns if t.get("status") == "PENDING")

        success_rate = (success_count / total * 100) if total > 0 else 0

        if success_rate >= 95:
            health = "excellent"
            health_desc = (
                "Transaction processing is healthy with very high success rates."
            )
        elif success_rate >= 85:
            health = "good"
            health_desc = "Transaction processing is mostly healthy but has some failures worth monitoring."
        elif success_rate >= 70:
            health = "concerning"
            health_desc = (
                "Transaction failure rate is elevated and warrants investigation."
            )
        else:
            health = "critical"
            health_desc = (
                "Transaction failure rate is critical and requires immediate attention."
            )

        insights.append(
            {
                "category": "overall_health",
                "status": health,
                "metric": f"{success_rate:.1f}% success rate",
                "description": health_desc,
                "action": None
                if health in ("excellent", "good")
                else "Review failed transactions and identify common failure reasons",
            }
        )

        # Anomaly insights
        anomaly_txns = [t for t in txns if t.get("isAnomaly")]
        if anomaly_txns:
            insights.append(
                {
                    "category": "anomalies",
                    "status": "warning",
                    "metric": f"{len(anomaly_txns)} flagged transactions",
                    "description": f"Detected {len(anomaly_txns)} transaction(s) with unusual characteristics (large amounts, duplicates, or high-frequency accounts).",
                    "action": "Review flagged transactions for potential fraud or processing errors",
                }
            )

        # Pattern insights
        recurring = [p for p in patterns if p.get("type") == "recurring_payment"]
        if recurring:
            insights.append(
                {
                    "category": "patterns",
                    "status": "info",
                    "metric": f"{len(recurring)} recurring payment patterns",
                    "description": f"Identified {len(recurring)} likely recurring payment pattern(s) (salaries, subscriptions, etc.).",
                    "action": None,
                }
            )

        off_hours = [p for p in patterns if p.get("type") == "off_hours_activity"]
        if off_hours:
            insights.append(
                {
                    "category": "patterns",
                    "status": "warning",
                    "metric": f"{off_hours[0].get('off_hours_count', 0)} off-hours transactions",
                    "description": off_hours[0].get(
                        "insight", "Unusual off-hours activity detected."
                    ),
                    "action": "Verify these transactions are legitimate batch processes or investigate for unauthorized activity",
                }
            )

        # Gap insights
        high_severity_gaps = [g for g in gaps if g.get("severity") == "high"]
        if high_severity_gaps:
            for gap in high_severity_gaps:
                insights.append(
                    {
                        "category": "gaps",
                        "status": "critical",
                        "metric": gap.get("type", "unknown gap"),
                        "description": gap.get("insight", "Critical gap detected"),
                        "action": "Immediate investigation required",
                    }
                )

        # Resolution status
        if unresolved:
            insights.append(
                {
                    "category": "resolution",
                    "status": "pending",
                    "metric": f"{len(unresolved)} unresolved transactions",
                    "description": f"{len(unresolved)} transaction(s) remain in PENDING status after investigation attempts.",
                    "action": "Follow up with Interswitch support or wait for batch settlement",
                }
            )

        shared_data["insights"] = insights
        return {
            "insights": insights,
            "total_insights": len(insights),
            "health_status": health,
            "recommendations_count": sum(1 for i in insights if i.get("action")),
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
        # ── NEW INTELLIGENT ANALYSIS TOOLS ──
        Tool(
            name="analyze_patterns",
            description="Identify recurring payments, day-of-week patterns, time clustering, and settlement delay patterns in transactions.",
            parameters=[],
            execute=analyze_patterns,
        ),
        Tool(
            name="identify_gaps",
            description="Find missing expected transactions, date gaps, failed amounts, and stale pending transactions.",
            parameters=[
                ToolParam(
                    name="expected_count",
                    param_type=ToolParamType.INTEGER,
                    description="Expected number of transactions (0 to skip count check)",
                    required=False,
                    default=0,
                ),
            ],
            execute=identify_gaps,
        ),
        Tool(
            name="explain_findings",
            description="Generate human-readable insights and actionable recommendations from all detected anomalies, patterns, and gaps.",
            parameters=[],
            execute=explain_findings,
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
4. Run anomaly detection to flag unusual transactions
5. Use analyze_patterns to identify recurring payments and time-based patterns
6. Use identify_gaps to find missing or delayed transactions
7. Use explain_findings to generate actionable insights from all detected issues
8. Flag any remaining unresolved transactions
9. Produce your final reconciliation summary as JSON with all patterns, gaps, and insights"""

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

            # Gather intelligent analysis results
            patterns = shared_data.get("patterns", [])
            gaps = shared_data.get("gaps", [])
            insights = shared_data.get("insights", [])

            await self.emit_progress(
                f"Reconciliation complete — {len(transactions)} txns, "
                f"{len(resolved_refs)} resolved, {len(unresolved_refs)} unresolved, "
                f"{len(patterns)} patterns, {len(gaps)} gaps identified"
            )

            audit_entries: list[dict] = [
                {
                    "agent_type": "reconciliation",
                    "action": "reconciliation_complete",
                    "detail": {
                        "total_transactions": len(transactions),
                        "unresolved_count": len(unresolved_refs),
                        "resolved_count": len(resolved_refs),
                        "patterns_found": len(patterns),
                        "gaps_found": len(gaps),
                        "insights_count": len(insights),
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
                "reconciliation_patterns": patterns,
                "reconciliation_gaps": gaps,
                "reconciliation_insights": insights,
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

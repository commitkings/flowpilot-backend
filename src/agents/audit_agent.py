import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry

logger = logging.getLogger(__name__)

# Maps internal risk reason slugs (from RiskAgent / tools) to short SME-facing sentences.
_RISK_REASON_PLAIN: dict[str, str] = {
    "high amount deviation": (
        "The amount stood out compared with what we could learn from your recent "
        "payout patterns, so we treated it with extra care."
    ),
    "new beneficiary": (
        "This recipient did not look like someone you pay often through this flow, "
        "so there was less past success to lean on."
    ),
    "duplicate similarity": (
        "This payment looked very similar to another one on file, so we wanted "
        "to be sure it was not a double payment."
    ),
    "velocity": (
        "The timing or frequency of this payment looked unusual compared with "
        "recent activity."
    ),
    "transaction_data_unavailable": (
        "We could not load enough bank transaction history for the period you chose, "
        "so the review leaned more on the payout details themselves."
    ),
}


def _normalize_risk_reason_list(reasons: Any) -> list[str]:
    if reasons is None:
        return []
    if isinstance(reasons, list):
        out: list[str] = []
        for r in reasons:
            if r is None:
                continue
            s = str(r).strip()
            if not s:
                continue
            out.extend(part.strip() for part in s.split("|") if part.strip())
        return out
    s = str(reasons).strip()
    if not s:
        return []
    return [part.strip() for part in s.split("|") if part.strip()]


def _plain_language_risk_drivers(reasons: Any) -> list[str]:
    """Turn scorer reason codes into plain-language lines for the audit narrative."""
    normalized = _normalize_risk_reason_list(reasons)
    lines: list[str] = []
    seen: set[str] = set()
    for raw in normalized:
        key = raw.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        mapped = _RISK_REASON_PLAIN.get(key)
        if mapped:
            lines.append(mapped)
        else:
            friendly = raw.replace("_", " ").strip()
            lines.append(
                f"Our checks picked up the following signal: {friendly}. "
                "That contributed to the overall reading for this person."
            )
    return lines


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_ngn(value: Any) -> str:
    return f"NGN {_safe_float(value):,.2f}"


def _join_plain(items: list[str]) -> str:
    clean = [item.strip() for item in items if item and item.strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return ", ".join(clean[:-1]) + f", and {clean[-1]}"


def _account_suffix(value: Any) -> str:
    text = str(value or "").strip()
    return text[-3:] if len(text) >= 3 else text or "unknown"


def _display_institution(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.isdigit():
        return None
    return text


def _lookup_status_summary(detail: dict[str, Any]) -> str:
    status = str(detail.get("lookup_status", "")).lower()
    lookup_name = str(detail.get("lookup_name") or "").strip()
    if status == "success":
        if lookup_name:
            return (
                f"We confirmed the account holder name with the bank, and it came back as "
                f"{lookup_name}."
            )
        return "We confirmed the account holder name with the bank, and it matched."
    if status in {"mismatch", "name_mismatch"}:
        return (
            "We reached the bank, but the returned account holder name did not match the "
            "name entered for this recipient."
        )
    if status == "failed":
        return "We could not confirm the account holder name with the bank."
    if status == "not_performed":
        return "We did not run a bank-name confirmation for this recipient."
    return "The bank-name confirmation outcome for this recipient was unclear."


def _execution_status_summary(detail: dict[str, Any]) -> str:
    exec_status = str(detail.get("execution_status", "")).lower()
    lookup_status = str(detail.get("lookup_status", "")).lower()
    decision = str(detail.get("risk_decision", "")).lower()

    if exec_status == "success":
        return "The payout was sent successfully."
    if exec_status == "pending":
        return "The payout request was submitted and is still awaiting a final completion update."
    if exec_status == "failed":
        return "A payout attempt was made, but it did not complete successfully."
    if lookup_status in {"failed", "mismatch", "name_mismatch"}:
        return "Because the bank-name confirmation did not pass, no payout was sent."
    if decision == "block":
        return "Because this recipient was held back under the safety rules, no payout was sent."
    if decision == "review":
        return "After review, this recipient still did not reach a payable state in this run."
    return "No payout was sent for this recipient in this run."


def _build_deterministic_executive_summary(bundle: dict[str, Any]) -> str:
    candidate_details = list(bundle.get("candidate_details") or [])
    risk_tolerance = _safe_float(bundle.get("risk_tolerance_used"), 0.35)
    payout_mode = str(bundle.get("payout_mode") or "live")
    objective = str(bundle.get("objective") or "this payout run").strip()
    metrics = bundle.get("metrics") or {}

    total_candidates = len(candidate_details)
    approved_count = int(metrics.get("total_approved") or 0)
    success_count = int(metrics.get("execution_success") or 0)
    pending_count = int(metrics.get("execution_pending") or 0)
    failed_count = int(metrics.get("execution_failed") or 0)
    total_requested_amount = _safe_float(metrics.get("total_requested_amount"))
    total_paid_amount = _safe_float(metrics.get("total_amount_disbursed"))

    if not candidate_details:
        parts = [
            f"This run was created for {objective}, but there were no recipients in the summary bundle.",
        ]
        if payout_mode == "simulated":
            parts.append("This was a test run, so no real money was sent.")
        return " ".join(parts)

    named_people = [str(detail.get("name") or "Unknown recipient") for detail in candidate_details[:5]]
    if total_candidates <= 5:
        people_phrase = f"{total_candidates} recipient(s): {_join_plain(named_people)}"
    else:
        people_phrase = f"{total_candidates} recipients"

    score_list: list[str] = []
    for detail in candidate_details:
        score = detail.get("risk_score")
        if isinstance(score, (int, float)):
            score_list.append(f"{float(score):.2f}")

    review_count = sum(
        1 for detail in candidate_details if str(detail.get("risk_decision", "")).lower() == "review"
    )
    block_count = sum(
        1 for detail in candidate_details if str(detail.get("risk_decision", "")).lower() == "block"
    )
    lookup_failed_count = sum(
        1
        for detail in candidate_details
        if str(detail.get("lookup_status", "")).lower() in {"failed", "mismatch", "name_mismatch"}
    )

    parts = [
        (
            f"This run reviewed {people_phrase} for {objective}, with a planned total of "
            f"{_format_ngn(total_requested_amount)}."
        )
    ]

    if score_list:
        if review_count == total_candidates:
            parts.append(
                f"The scores came in at {_join_plain(score_list)} against your {risk_tolerance:.2f} "
                f"comfort setting, so every recipient was held for human review before money could move."
            )
        else:
            parts.append(
                f"The scores came in at {_join_plain(score_list)} against your {risk_tolerance:.2f} "
                "comfort setting, which led to a mix of automatic holds and review decisions."
            )

    if approved_count:
        parts.append(
            f"You approved {approved_count} recipient(s) after review so the run could continue."
        )

    scoring_context_note = str(bundle.get("scoring_context_note") or "").strip()
    if scoring_context_note:
        parts.append(scoring_context_note)

    for detail in candidate_details:
        name = str(detail.get("name") or "Unknown recipient")
        amount = _format_ngn(detail.get("amount"))
        account_suffix = _account_suffix(detail.get("account_masked"))
        institution = _display_institution(detail.get("institution"))
        score = detail.get("risk_score")
        score_text = (
            f"{float(score):.2f}"
            if isinstance(score, (int, float))
            else str(score or "unknown")
        )
        intro = f"For {name}, amount {amount}"
        if institution:
            intro += f", through {institution}"
        intro += f", account ending in {account_suffix}"

        drivers = list(detail.get("what_drove_the_score") or [])
        driver_text = _join_plain(drivers)
        candidate_parts = [
            f"{intro}.",
            (
                f"The score was {score_text} against your {risk_tolerance:.2f} comfort setting, "
                f"so this recipient {detail.get('decision_in_plain_words', 'needed extra review')}."
            ),
        ]
        if driver_text:
            candidate_parts.append(f"The main reasons were: {driver_text}")
        tolerance_explanation = str(detail.get("tolerance_explanation") or "").strip()
        if tolerance_explanation:
            candidate_parts.append(tolerance_explanation)
        candidate_parts.append(_lookup_status_summary(detail))
        candidate_parts.append(_execution_status_summary(detail))
        parts.append(" ".join(candidate_parts))

    if total_paid_amount > 0:
        parts.append(
            f"In the end, {_format_ngn(total_paid_amount)} was sent successfully."
        )
    elif pending_count > 0:
        parts.append(
            f"No payout has fully completed yet, and {pending_count} payout(s) are still awaiting a final update."
        )
    else:
        parts.append("No payout was completed in this run.")

    if failed_count > 0 or lookup_failed_count > 0 or block_count > 0:
        parts.append(
            f"The run finished with exceptions because {lookup_failed_count} recipient(s) could not be confirmed "
            f"with the bank, {failed_count} payout attempt(s) failed after submission, and {success_count} of "
            f"{approved_count or total_candidates} approved payout(s) reached a successful finish."
        )
    else:
        parts.append("The run finished cleanly with no unresolved payout issues.")

    if payout_mode == "simulated":
        parts.append("This was a test run, so no real money was sent.")

    return " ".join(parts)


def _build_executive_summary_bundle(state: AgentState) -> dict[str, Any]:
    candidates = state.get("scored_candidates") or []
    exec_results = state.get("candidate_execution_results") or []
    lookup_results = state.get("candidate_lookup_results") or []
    transactions = state.get("transactions") or []
    risk_tolerance = state.get("risk_tolerance") or 0.35
    batch_details = state.get("batch_details") or {}
    approved_ids = {str(candidate_id) for candidate_id in state.get("approved_candidate_ids", [])}
    rejected_ids = {str(candidate_id) for candidate_id in state.get("rejected_candidate_ids", [])}

    success_exec = sum(
        1 for er in exec_results if er.get("execution_status") == "success"
    )
    pending_exec = sum(
        1 for er in exec_results if er.get("execution_status") == "pending"
    )
    failed_exec = sum(
        1 for er in exec_results if er.get("execution_status") == "failed"
    )

    total_requested_amount = sum(
        _safe_float(candidate.get("amount")) for candidate in candidates
    )
    total_disbursed_amount = _safe_float(batch_details.get("total_amount"))

    candidate_details: list[dict[str, Any]] = []
    for c in candidates:
        cid = str(c.get("candidate_id", "") or "")
        name = c.get("beneficiary_name", "Unknown")
        score = c.get("risk_score", 0)
        decision = c.get("risk_decision", "unknown")
        amount = c.get("amount", 0)
        institution = c.get("institution_code", "?")
        account = c.get("account_number", "?")
        reasons = c.get("risk_reasons", [])
        reason_list = _normalize_risk_reason_list(
            reasons if isinstance(reasons, list) else reasons
        )
        what_drove = _plain_language_risk_drivers(reason_list)

        score_f: float | None
        try:
            score_f = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_f = None
        tol_f = float(risk_tolerance) if risk_tolerance is not None else 0.35

        if score_f is not None and score_f > tol_f:
            tolerance_explanation = (
                f"The overall risk reading was {score_f:.2f}, which is above your "
                f"comfort setting of {tol_f:.2f}. That is why this payment was marked "
                f"for review before it could continue."
            )
        elif score_f is not None:
            tolerance_explanation = (
                f"The overall risk reading was {score_f:.2f}, at or below your "
                f"comfort setting of {tol_f:.2f} for automatic treatment."
            )
        else:
            tolerance_explanation = (
                "The overall risk reading could not be compared numerically to your "
                "comfort setting in this summary bundle."
            )

        decision_plain = {
            "allow": "was cleared to move forward under automatic rules",
            "review": "needed a human review before money could move",
            "block": "was held back under automatic rules",
        }.get(str(decision).lower(), "had an unclear automatic status")

        lookup = next(
            (lr for lr in lookup_results if str(lr.get("candidate_id", "")) == cid),
            {},
        )
        lookup_status = lookup.get("lookup_status", "not_performed")
        lookup_name = lookup.get("lookup_account_name", "")
        match_score = lookup.get("lookup_match_score")

        exec_result = next(
            (er for er in exec_results if str(er.get("candidate_id", "")) == cid),
            {},
        )
        exec_status = exec_result.get("execution_status", "not_executed")

        candidate_details.append(
            {
                "name": name,
                "amount": amount,
                "institution": institution,
                "account_masked": f"***{account[-3:]}" if len(account) >= 3 else account,
                "risk_score": round(score_f, 2) if score_f is not None else score,
                "risk_tolerance": risk_tolerance,
                "risk_decision": decision,
                "risk_reasons_raw": reason_list,
                "what_drove_the_score": what_drove,
                "tolerance_explanation": tolerance_explanation,
                "decision_in_plain_words": decision_plain,
                "score_vs_tolerance": (
                    f"{score_f:.2f} vs {tol_f:.2f} threshold"
                    if score_f is not None
                    else "?"
                ),
                "lookup_status": lookup_status,
                "lookup_name": lookup_name,
                "name_match_score": match_score,
                "execution_status": exec_status,
                "approved_after_review": cid in approved_ids,
                "rejected_after_review": cid in rejected_ids,
            }
        )

    payout_mode = (
        "simulated" if batch_details.get("batch_reference", "").startswith("FP_") else "live"
    )
    dq_flags = state.get("data_quality_flags") or []
    recon_degraded = any(
        f.get("flag") == "transaction_data_unavailable" for f in dq_flags
    )
    scoring_context_note = None
    if recon_degraded:
        scoring_context_note = (
            "We could not load recent bank transaction history for the selected period, "
            "so the review leaned more on who was being paid, how much, and how new "
            "each recipient looked in this flow."
        )

    state_hash = hashlib.sha256(
        json.dumps(state, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    bundle = {
        "run_id": state.get("run_id"),
        "objective": state.get("objective"),
        "payout_mode": payout_mode,
        "risk_tolerance_used": risk_tolerance,
        "reconciliation_degraded": recon_degraded,
        "scoring_context_note": scoring_context_note,
        "total_bank_transactions": len(transactions),
        "candidate_details": candidate_details,
        "metrics": {
            "total_candidates_scored": len(candidates),
            "total_approved": len(approved_ids),
            "total_rejected": len(rejected_ids),
            "total_executed": len(exec_results),
            "execution_success": success_exec,
            "execution_pending": pending_exec,
            "execution_failed": failed_exec,
            "success_rate": round(
                (success_exec + pending_exec) / max(len(exec_results), 1), 2
            ),
            "total_amount_disbursed": total_disbursed_amount,
            "total_requested_amount": total_requested_amount,
        },
        "data_integrity_hash": state_hash,
        "generated_at": datetime.utcnow().isoformat(),
    }
    bundle["executive_summary"] = _build_deterministic_executive_summary(bundle)
    return bundle


AUDIT_SYSTEM_PROMPT = """You are a financial audit and compliance analyst for FlowPilot.

Your job: perform deep analysis of the complete run data, audit risk decisions against outcomes, analyze costs, verify compliance, and produce a thorough audit report with actionable recommendations.

## Your workflow:
1. Use `get_run_timeline` to understand the full sequence of events in this run
2. Use `compute_risk_distribution` to analyze the risk scoring patterns
3. Use `analyze_risk_decisions` to audit each risk decision against actual outcomes
4. Use `compute_cost_analysis` to calculate fees, failed payment costs, and efficiency metrics
5. Use `check_compliance` to verify all required approvals and processes were followed
6. Use `compare_to_past_runs` to see how this run compares to historical norms
7. Use `detect_run_anomalies` to flag anything unusual
8. Use `generate_recommendations` to produce actionable suggestions based on all findings
9. Use `generate_executive_summary` to compile all findings into a structured report

## What makes a GOOD audit report:
- Quantitative facts, not vague statements
- Clear risk/compliance flags if any
- Comparison to baselines/norms
- Analysis of risk decisions vs outcomes (were blocked candidates actually risky? were allowed candidates successful?)
- Cost breakdown (fees, failed payment costs, retry costs)
- Specific, actionable recommendations
- Data integrity verification

## Decision Analysis Questions to Answer:
- Were risk scores accurate predictors of outcomes?
- Did any "allowed" candidates fail? Why?
- Were any "blocked" candidates manually approved? What happened?
- Is the risk threshold appropriately calibrated?
- Are there patterns in failures that could improve future scoring?

## CRITICAL: Executive Summary Guidelines
The `executive_summary` field is the most important part of the report. It is shown prominently to the user.
It MUST be detailed, specific, and actionable. NEVER write generic boilerplate.

STYLE RULES (MANDATORY):
- NEVER use em dashes (the long dash character). Use commas, periods, or semicolons instead.
- NEVER mention technical details like API errors, HTTP status codes (401, 500), service names (Interswitch, BAV), or internal system names.
- Write for a non-technical business owner. Use plain, clear language.
- If bank verification failed, say "We could not confirm the account holder name with the bank" not "BAV returned 500".
- If transaction data was unavailable, say "No recent bank transactions were found for this period" not "Transaction Search returned 401".
- If it was a simulated run, say "This was a test run, no real money was sent."

A GOOD executive summary includes:
- The exact purpose and number of recipients (by name if 5 or fewer)
- The total amount with currency symbol (use the Naira sign)
- Each candidate's risk score vs the tolerance you set, and whether they passed
- A dedicated explanation of **why** each score landed where it did: you MUST weave in the plain-language lines from `generate_executive_summary` tool output (`what_drove_the_score`, `tolerance_explanation`). Do not only repeat the numeric score.
- If `reconciliation_degraded` is true in that tool output, say clearly that payout history for the chosen dates could not be loaded, and that reviews leaned more on beneficiary and amount signals.
- Whether account names were confirmed with the bank
- Whether this was a test run or a real payout
- Any issues found, in plain language
- The overall result: did everything go well, or are there things to watch?

Example of a GOOD executive summary:
"This staff salary run processed a payment of N500,000.00 to 1 person (Michael John Doe, GTBank account ending in 000). The risk score came in at 0.17, which is below your 0.20 threshold, so they were approved automatically. We confirmed the account holder name with the bank and it matched. This was a test run, so no real money was sent. No recent bank transactions were found for the Feb 2025 period, which is expected for a new setup. All compliance checks passed."

Example of a BAD executive summary (DO NOT write like this):
"The FlowPilot run was executed with a 100% success rate. All candidates were approved and executed without any issues."

## Final answer format (JSON):
{
  "audit_report": {
    "executive_summary": "Detailed, specific narrative with amounts, names, scores, and outcomes as described above",
    "run_metrics": {
      "total_transactions": 0,
      "total_candidates": 0,
      "total_approved": 0,
      "total_executed": 0,
      "total_amount": 0.0,
      "success_rate": 0.0
    },
    "risk_analysis": {
      "average_risk_score": 0.0,
      "risk_distribution": {},
      "decision_accuracy": {},
      "flagged_items": []
    },
    "cost_analysis": {
      "total_fees": 0.0,
      "failed_payment_costs": 0.0,
      "efficiency_ratio": 0.0
    },
    "compliance_status": {
      "all_approvals_valid": true,
      "flags": []
    },
    "anomalies": [],
    "recommendations": [],
    "data_integrity_hash": "..."
  }
}
"""


def _build_audit_tools(state: AgentState, db_session=None) -> list[Tool]:
    async def get_run_timeline() -> dict[str, Any]:
        audit_entries = state.get("audit_entries", [])
        plan_steps = state.get("plan_steps", [])
        reasoning_log = state.get("reasoning_log", [])

        timeline = []
        for entry in audit_entries:
            timeline.append(
                {
                    "type": "audit_entry",
                    "agent": entry.get("agent_type"),
                    "action": entry.get("action"),
                    "detail_summary": str(entry.get("detail", {}))[:200],
                    "timestamp": entry.get("created_at"),
                }
            )

        return {
            "run_id": state.get("run_id"),
            "objective": state.get("objective"),
            "plan_steps": plan_steps,
            "timeline_entries": timeline,
            "reasoning_steps": len(reasoning_log),
            "total_audit_entries": len(audit_entries),
        }

    async def compute_risk_distribution() -> dict[str, Any]:
        candidates = state.get("scored_candidates", [])
        if not candidates:
            return {"total": 0, "note": "No candidates scored"}

        decisions: dict[str, int] = {}
        scores = []
        total_amount = 0.0
        amounts_by_decision: dict[str, float] = {}

        for c in candidates:
            d = c.get("risk_decision", "unknown")
            decisions[d] = decisions.get(d, 0) + 1
            score = c.get("risk_score", 0)
            scores.append(score)
            amount = c.get("amount", 0.0)
            total_amount += amount
            amounts_by_decision[d] = amounts_by_decision.get(d, 0) + amount

        avg_score = sum(scores) / len(scores) if scores else 0
        min_score = min(scores) if scores else 0
        max_score = max(scores) if scores else 0

        high_risk = [
            {
                "candidate_id": c.get("candidate_id"),
                "beneficiary_name": c.get("beneficiary_name"),
                "amount": c.get("amount"),
                "risk_score": c.get("risk_score"),
                "risk_reasons": c.get("risk_reasons", []),
            }
            for c in candidates
            if c.get("risk_score", 0) > 0.6
        ]

        return {
            "total_candidates": len(candidates),
            "decision_distribution": decisions,
            "amounts_by_decision": {
                k: round(v, 2) for k, v in amounts_by_decision.items()
            },
            "score_stats": {
                "average": round(avg_score, 3),
                "min": round(min_score, 3),
                "max": round(max_score, 3),
            },
            "total_amount": round(total_amount, 2),
            "high_risk_candidates": high_risk,
        }

    async def compare_to_past_runs() -> dict[str, Any]:
        if db_session is None:
            return {
                "note": "No DB session — cannot compare to past runs",
                "comparison": {},
            }

        try:
            from src.infrastructure.database.repositories.run_repository import (
                RunRepository,
            )
            from uuid import UUID

            repo = RunRepository(db_session)
            business_id = state.get("business_id")
            if not business_id:
                return {"error": "No business_id", "comparison": {}}

            bid = UUID(business_id) if isinstance(business_id, str) else business_id
            runs, total = await repo.list_by_business(bid, limit=10)

            completed_runs = [r for r in runs if r.status == "completed"]
            failed_runs = [r for r in runs if r.status == "failed"]

            current_candidates = len(state.get("scored_candidates", []))
            current_txns = len(state.get("transactions", []))

            return {
                "total_historical_runs": total,
                "recent_completed": len(completed_runs),
                "recent_failed": len(failed_runs),
                "historical_success_rate": round(
                    len(completed_runs) / max(len(runs), 1), 2
                ),
                "current_run": {
                    "transaction_count": current_txns,
                    "candidate_count": current_candidates,
                },
                "comparison_notes": "Current run metrics compared to historical averages",
            }
        except Exception as e:
            return {"error": str(e), "comparison": {}}

    async def detect_run_anomalies() -> dict[str, Any]:
        anomalies = []

        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])
        lookup_results = state.get("candidate_lookup_results", [])
        approved_ids = state.get("approved_candidate_ids", [])
        rejected_ids = state.get("rejected_candidate_ids", [])

        blocked = [c for c in candidates if c.get("risk_decision") == "block"]
        blocked_but_approved = [
            c for c in blocked if c.get("candidate_id") in set(approved_ids)
        ]
        if blocked_but_approved:
            anomalies.append(
                {
                    "type": "blocked_candidates_approved",
                    "severity": "high",
                    "detail": f"{len(blocked_but_approved)} candidates marked as 'block' were manually approved",
                    "candidate_ids": [
                        c.get("candidate_id") for c in blocked_but_approved
                    ],
                }
            )

        failed_lookups = [
            lr for lr in lookup_results if lr.get("lookup_status") == "failed"
        ]
        if len(failed_lookups) > len(lookup_results) * 0.5 and lookup_results:
            anomalies.append(
                {
                    "type": "high_lookup_failure_rate",
                    "severity": "medium",
                    "detail": f"{len(failed_lookups)}/{len(lookup_results)} lookups failed ({round(len(failed_lookups) / len(lookup_results) * 100)}%)",
                }
            )

        mismatches = [
            lr for lr in lookup_results if lr.get("lookup_status") == "mismatch"
        ]
        if mismatches:
            anomalies.append(
                {
                    "type": "name_mismatches",
                    "severity": "medium",
                    "detail": f"{len(mismatches)} beneficiary name mismatches detected",
                    "candidates": [m.get("candidate_id") for m in mismatches],
                }
            )

        failed_payouts = [
            er for er in exec_results if er.get("execution_status") == "failed"
        ]
        if failed_payouts:
            anomalies.append(
                {
                    "type": "failed_payouts",
                    "severity": "high" if len(failed_payouts) > 3 else "medium",
                    "detail": f"{len(failed_payouts)} payout(s) failed",
                    "candidate_ids": [er.get("candidate_id") for er in failed_payouts],
                }
            )

        if state.get("error"):
            anomalies.append(
                {
                    "type": "run_error",
                    "severity": "high",
                    "detail": state["error"],
                }
            )

        return {
            "anomalies": anomalies,
            "total_anomalies": len(anomalies),
            "severity_counts": {
                "high": sum(1 for a in anomalies if a.get("severity") == "high"),
                "medium": sum(1 for a in anomalies if a.get("severity") == "medium"),
                "low": sum(1 for a in anomalies if a.get("severity") == "low"),
            },
        }

    async def generate_executive_summary() -> dict[str, Any]:
        return _build_executive_summary_bundle(state)

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: analyze_risk_decisions — audit risk decisions vs outcomes
    # ────────────────────────────────────────────────────────────────────────
    async def analyze_risk_decisions() -> dict[str, Any]:
        """Analyze whether risk decisions were accurate predictors of execution outcomes."""
        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])
        approved_ids = set(state.get("approved_candidate_ids", []))
        rejected_ids = set(state.get("rejected_candidate_ids", []))

        if not candidates:
            return {"note": "No candidates to analyze", "analysis": {}}

        # Build execution outcome map
        exec_map: dict[str, str] = {}
        for er in exec_results:
            cid = er.get("candidate_id")
            if cid:
                exec_map[cid] = er.get("execution_status", "unknown")

        analysis = {
            "total_candidates": len(candidates),
            "decisions": {"allow": 0, "review": 0, "block": 0, "unknown": 0},
            "outcomes": {"success": 0, "failed": 0, "pending": 0, "not_executed": 0},
            "decision_accuracy": {
                "allowed_and_succeeded": 0,
                "allowed_and_failed": 0,  # False negatives
                "blocked_and_approved": 0,  # Manual overrides
                "blocked_approved_succeeded": 0,  # Override was correct
                "blocked_approved_failed": 0,  # Override was wrong
            },
            "false_negatives": [],  # Allowed but failed
            "risky_overrides": [],  # Blocked but approved
            "calibration_notes": [],
        }

        for c in candidates:
            cid = c.get("candidate_id")
            decision = c.get("risk_decision", "unknown")
            score = c.get("risk_score", 0)
            exec_status = (
                exec_map.get(str(cid), "not_executed") if cid else "not_executed"
            )

            # Count decisions
            if decision in analysis["decisions"]:
                analysis["decisions"][decision] += 1
            else:
                analysis["decisions"]["unknown"] += 1

            # Count outcomes
            if exec_status in analysis["outcomes"]:
                analysis["outcomes"][exec_status] += 1
            else:
                analysis["outcomes"]["not_executed"] += 1

            # Decision accuracy analysis
            if decision == "allow":
                if exec_status == "success":
                    analysis["decision_accuracy"]["allowed_and_succeeded"] += 1
                elif exec_status == "failed":
                    analysis["decision_accuracy"]["allowed_and_failed"] += 1
                    analysis["false_negatives"].append(
                        {
                            "candidate_id": cid,
                            "beneficiary_name": c.get("beneficiary_name"),
                            "amount": c.get("amount"),
                            "risk_score": score,
                            "failure_reason": "Allowed by risk scoring but execution failed",
                        }
                    )

            elif decision == "block":
                if cid in approved_ids:
                    analysis["decision_accuracy"]["blocked_and_approved"] += 1
                    if exec_status == "success":
                        analysis["decision_accuracy"]["blocked_approved_succeeded"] += 1
                        analysis["risky_overrides"].append(
                            {
                                "candidate_id": cid,
                                "beneficiary_name": c.get("beneficiary_name"),
                                "amount": c.get("amount"),
                                "risk_score": score,
                                "outcome": "success",
                                "note": "Override was justified - execution succeeded",
                            }
                        )
                    elif exec_status == "failed":
                        analysis["decision_accuracy"]["blocked_approved_failed"] += 1
                        analysis["risky_overrides"].append(
                            {
                                "candidate_id": cid,
                                "beneficiary_name": c.get("beneficiary_name"),
                                "amount": c.get("amount"),
                                "risk_score": score,
                                "outcome": "failed",
                                "note": "RISKY: Override failed - risk scoring was correct",
                            }
                        )

        # Calibration recommendations
        false_neg_rate = analysis["decision_accuracy"]["allowed_and_failed"] / max(
            analysis["decisions"]["allow"], 1
        )
        if false_neg_rate > 0.1:
            analysis["calibration_notes"].append(
                {
                    "issue": "high_false_negative_rate",
                    "rate": f"{false_neg_rate:.1%}",
                    "recommendation": "Consider lowering the 'allow' threshold - too many allowed candidates are failing",
                }
            )

        override_failure_rate = analysis["decision_accuracy"][
            "blocked_approved_failed"
        ] / max(analysis["decision_accuracy"]["blocked_and_approved"], 1)
        if (
            analysis["decision_accuracy"]["blocked_and_approved"] > 0
            and override_failure_rate > 0.3
        ):
            analysis["calibration_notes"].append(
                {
                    "issue": "risky_overrides_failing",
                    "rate": f"{override_failure_rate:.1%}",
                    "recommendation": "Manual overrides of blocked candidates have high failure rate - review approval process",
                }
            )

        return analysis

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: compute_cost_analysis — fees, failed costs, efficiency
    # ────────────────────────────────────────────────────────────────────────
    async def compute_cost_analysis() -> dict[str, Any]:
        """Calculate total fees, failed payment costs, and efficiency metrics."""
        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])

        # Default fee structure (could be made configurable via business_config)
        TRANSFER_FEE_PCT = 0.005  # 0.5% fee
        TRANSFER_FEE_CAP = 500.0  # Max ₦500 per transaction
        FAILED_RETRY_COST = 50.0  # Cost to retry a failed payment

        total_attempted_amount = 0.0
        total_successful_amount = 0.0
        total_failed_amount = 0.0
        total_pending_amount = 0.0

        fees_on_success = 0.0
        fees_on_failed = 0.0  # Fees charged even on failures
        retry_costs = 0.0

        for er in exec_results:
            amount = er.get("amount", 0)
            if isinstance(amount, Decimal):
                amount = float(amount)
            status = er.get("execution_status", "")

            total_attempted_amount += amount
            fee = min(amount * TRANSFER_FEE_PCT, TRANSFER_FEE_CAP)

            if status == "success":
                total_successful_amount += amount
                fees_on_success += fee
            elif status == "failed":
                total_failed_amount += amount
                fees_on_failed += fee  # Some providers charge on attempt
                retry_costs += FAILED_RETRY_COST
            elif status == "pending":
                total_pending_amount += amount

        # Efficiency metrics
        execution_efficiency = total_successful_amount / max(total_attempted_amount, 1)
        cost_per_successful_ngn = (
            (fees_on_success + fees_on_failed + retry_costs)
            / max(total_successful_amount, 1)
            * 1000  # Per ₦1000
        )

        return {
            "amounts": {
                "total_attempted": round(total_attempted_amount, 2),
                "total_successful": round(total_successful_amount, 2),
                "total_failed": round(total_failed_amount, 2),
                "total_pending": round(total_pending_amount, 2),
            },
            "costs": {
                "fees_on_successful": round(fees_on_success, 2),
                "fees_on_failed": round(fees_on_failed, 2),
                "retry_costs": round(retry_costs, 2),
                "total_costs": round(fees_on_success + fees_on_failed + retry_costs, 2),
            },
            "efficiency": {
                "execution_efficiency": f"{execution_efficiency:.1%}",
                "cost_per_1000_ngn_success": round(cost_per_successful_ngn, 2),
                "wasted_on_failures": round(fees_on_failed + retry_costs, 2),
            },
            "transaction_counts": {
                "total_executed": len(exec_results),
                "successful": sum(
                    1 for er in exec_results if er.get("execution_status") == "success"
                ),
                "failed": sum(
                    1 for er in exec_results if er.get("execution_status") == "failed"
                ),
                "pending": sum(
                    1 for er in exec_results if er.get("execution_status") == "pending"
                ),
            },
        }

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: check_compliance — verify approvals and processes
    # ────────────────────────────────────────────────────────────────────────
    async def check_compliance() -> dict[str, Any]:
        """Verify all required approvals happened and compliance requirements were met."""
        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])
        approved_ids = set(state.get("approved_candidate_ids", []))
        rejected_ids = set(state.get("rejected_candidate_ids", []))
        budget_cap = state.get("budget_cap", 0)

        compliance_flags: list[dict[str, Any]] = []
        checks_passed: list[str] = []

        # 1. Check: All executed candidates were approved
        executed_ids = {er.get("candidate_id") for er in exec_results}
        executed_without_approval = executed_ids - approved_ids
        if executed_without_approval:
            compliance_flags.append(
                {
                    "check": "approval_required",
                    "status": "FAILED",
                    "severity": "critical",
                    "detail": f"{len(executed_without_approval)} candidate(s) executed without explicit approval",
                    "candidate_ids": list(executed_without_approval)[:5],
                }
            )
        else:
            checks_passed.append("All executed candidates had prior approval")

        # 2. Check: No blocked candidates executed without override
        blocked_candidates = {
            c.get("candidate_id")
            for c in candidates
            if c.get("risk_decision") == "block"
        }
        blocked_and_executed = blocked_candidates & executed_ids
        blocked_without_override = blocked_and_executed - approved_ids
        if blocked_without_override:
            compliance_flags.append(
                {
                    "check": "blocked_execution",
                    "status": "FAILED",
                    "severity": "critical",
                    "detail": f"{len(blocked_without_override)} blocked candidate(s) were executed without manual override",
                    "candidate_ids": list(blocked_without_override)[:5],
                }
            )
        else:
            checks_passed.append("No blocked candidates executed without override")

        # 3. Check: Budget compliance
        if budget_cap and budget_cap > 0:
            total_executed_amount = sum(
                er.get("amount", 0)
                for er in exec_results
                if er.get("execution_status") == "success"
            )
            if total_executed_amount > float(budget_cap):
                compliance_flags.append(
                    {
                        "check": "budget_cap",
                        "status": "FAILED",
                        "severity": "high",
                        "detail": f"Total executed (₦{total_executed_amount:,.2f}) exceeds budget cap (₦{float(budget_cap):,.2f})",
                        "over_by": round(total_executed_amount - float(budget_cap), 2),
                    }
                )
            else:
                budget_utilization = total_executed_amount / float(budget_cap) * 100
                checks_passed.append(
                    f"Within budget cap ({budget_utilization:.1f}% utilized)"
                )

        # 4. Check: Risk scoring was performed
        candidates_without_scores = [
            c for c in candidates if c.get("risk_score") is None
        ]
        if candidates_without_scores:
            compliance_flags.append(
                {
                    "check": "risk_scoring",
                    "status": "WARNING",
                    "severity": "medium",
                    "detail": f"{len(candidates_without_scores)} candidate(s) lack risk scores",
                }
            )
        else:
            checks_passed.append("All candidates have risk scores")

        # 5. Check: High-value transactions had extra scrutiny
        HIGH_VALUE_THRESHOLD = 1_000_000  # ₦1M
        high_value_candidates = [
            c for c in candidates if c.get("amount", 0) >= HIGH_VALUE_THRESHOLD
        ]
        high_value_allowed = [
            c for c in high_value_candidates if c.get("risk_decision") == "allow"
        ]
        if high_value_allowed:
            compliance_flags.append(
                {
                    "check": "high_value_review",
                    "status": "WARNING",
                    "severity": "low",
                    "detail": f"{len(high_value_allowed)} high-value (>₦1M) candidate(s) were auto-allowed - consider manual review requirement",
                }
            )

        return {
            "all_checks_passed": len(compliance_flags) == 0,
            "checks_passed": checks_passed,
            "compliance_flags": compliance_flags,
            "critical_issues": sum(
                1 for f in compliance_flags if f.get("severity") == "critical"
            ),
            "warnings": sum(
                1
                for f in compliance_flags
                if f.get("severity") in ("medium", "low", "high")
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # NEW TOOL: generate_recommendations — actionable suggestions
    # ────────────────────────────────────────────────────────────────────────
    async def generate_recommendations() -> dict[str, Any]:
        """Generate actionable recommendations based on all audit findings."""
        candidates = state.get("scored_candidates", [])
        exec_results = state.get("candidate_execution_results", [])
        reconciliation_insights = state.get("reconciliation_insights", [])

        recommendations: list[dict[str, Any]] = []

        # Analyze failure patterns
        failed_results = [
            er for er in exec_results if er.get("execution_status") == "failed"
        ]
        if failed_results:
            failure_rate = len(failed_results) / max(len(exec_results), 1)
            if failure_rate > 0.1:
                recommendations.append(
                    {
                        "category": "execution",
                        "priority": "high",
                        "issue": f"High failure rate ({failure_rate:.1%})",
                        "recommendation": "Review failed transactions for common patterns (invalid accounts, insufficient funds, bank issues)",
                        "expected_impact": "Reduce failed payment costs and improve execution efficiency",
                    }
                )

        # Analyze risk scoring patterns
        scores = [
            c.get("risk_score", 0)
            for c in candidates
            if c.get("risk_score") is not None
        ]
        if scores:
            avg_score = sum(scores) / len(scores)
            if avg_score > 0.5:
                recommendations.append(
                    {
                        "category": "risk",
                        "priority": "medium",
                        "issue": f"Average risk score is high ({avg_score:.2f})",
                        "recommendation": "Review beneficiary data quality - high scores may indicate incomplete or inconsistent data",
                        "expected_impact": "Reduce false positives and improve processing speed",
                    }
                )

            # Check for score clustering
            allow_scores = [
                c.get("risk_score", 0)
                for c in candidates
                if c.get("risk_decision") == "allow"
            ]
            review_scores = [
                c.get("risk_score", 0)
                for c in candidates
                if c.get("risk_decision") == "review"
            ]
            if allow_scores and review_scores:
                if max(allow_scores) > 0.25:
                    recommendations.append(
                        {
                            "category": "risk_calibration",
                            "priority": "medium",
                            "issue": "Some 'allow' decisions have relatively high scores",
                            "recommendation": "Consider lowering the allow threshold for stricter risk control",
                            "expected_impact": "Reduce potential fraud exposure",
                        }
                    )

        # Analyze reconciliation insights
        critical_insights = [
            i
            for i in reconciliation_insights
            if i.get("status") in ("critical", "warning")
        ]
        if critical_insights:
            recommendations.append(
                {
                    "category": "reconciliation",
                    "priority": "high",
                    "issue": f"{len(critical_insights)} critical/warning issue(s) from reconciliation",
                    "recommendation": "Address reconciliation findings before next run",
                    "expected_impact": "Improve data quality and reduce anomalies",
                }
            )

        # Process efficiency
        if len(exec_results) > 0:
            success_count = sum(
                1 for er in exec_results if er.get("execution_status") == "success"
            )
            if success_count == len(exec_results):
                recommendations.append(
                    {
                        "category": "positive",
                        "priority": "info",
                        "issue": "Perfect execution success rate",
                        "recommendation": "Current process is working well - maintain current practices",
                        "expected_impact": "N/A",
                    }
                )

        # Sort by priority
        priority_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
        recommendations.sort(
            key=lambda r: priority_order.get(r.get("priority", "info"), 99)
        )

        return {
            "recommendations": recommendations,
            "total_recommendations": len(recommendations),
            "high_priority_count": sum(
                1 for r in recommendations if r.get("priority") == "high"
            ),
        }

    return [
        Tool(
            name="get_run_timeline",
            description="Get the complete timeline of events, plan steps, and reasoning for this run.",
            parameters=[],
            execute=get_run_timeline,
        ),
        Tool(
            name="compute_risk_distribution",
            description="Analyze risk scoring patterns: distribution of decisions, score statistics, high-risk candidates.",
            parameters=[],
            execute=compute_risk_distribution,
        ),
        # ── NEW INTELLIGENT AUDIT TOOLS ──
        Tool(
            name="analyze_risk_decisions",
            description="Audit risk decisions against execution outcomes. Find false negatives (allowed but failed) and evaluate manual overrides.",
            parameters=[],
            execute=analyze_risk_decisions,
        ),
        Tool(
            name="compute_cost_analysis",
            description="Calculate total fees, failed payment costs, retry costs, and efficiency metrics.",
            parameters=[],
            execute=compute_cost_analysis,
        ),
        Tool(
            name="check_compliance",
            description="Verify all required approvals happened, budget compliance, and audit trail completeness.",
            parameters=[],
            execute=check_compliance,
        ),
        Tool(
            name="compare_to_past_runs",
            description="Compare this run's metrics to historical runs for the same business.",
            parameters=[],
            execute=compare_to_past_runs,
        ),
        Tool(
            name="detect_run_anomalies",
            description="Detect anomalies in the run: blocked-but-approved candidates, high failure rates, name mismatches, etc.",
            parameters=[],
            execute=detect_run_anomalies,
        ),
        Tool(
            name="generate_recommendations",
            description="Generate actionable recommendations based on all audit findings, categorized by priority.",
            parameters=[],
            execute=generate_recommendations,
        ),
        Tool(
            name="generate_executive_summary",
            description="Compile all run metrics into a structured summary with data integrity hash.",
            parameters=[],
            execute=generate_executive_summary,
        ),
    ]


class AuditAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("AuditAgent")

    async def run(self, state: AgentState, db_session=None) -> AgentState:
        logger.info(
            f"[AuditAgent] Generating audit report for run {state.get('run_id')}"
        )

        self.registry = ToolRegistry()
        for tool in _build_audit_tools(state, db_session):
            self.registry.register(tool)

        user_prompt = f"""Generate a comprehensive audit report for this FlowPilot run.

Run ID: {state.get("run_id")}
Objective: {state.get("objective")}

Use your tools in this order:
1. Get the run timeline to understand what happened
2. Analyze risk scoring distribution
3. Use analyze_risk_decisions to audit risk decisions against actual outcomes
4. Use compute_cost_analysis to calculate fees and efficiency metrics
5. Use check_compliance to verify approvals and compliance requirements
6. Compare to past runs (if DB available)
7. Detect any anomalies in the run
8. Use generate_recommendations to produce actionable suggestions
9. Generate executive summary with all metrics

Then produce the final audit report JSON with all findings, costs, compliance status, and recommendations."""

        try:
            await self.emit_progress(
                "Analyzing run data and generating audit report..."
            )

            response = await self.reason_and_act_json(
                system_prompt=AUDIT_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

            try:
                report = json.loads(response)
            except json.JSONDecodeError:
                report = {"audit_report": {"raw_response": response}}

            if "audit_report" in report:
                audit_report = report["audit_report"]
            else:
                audit_report = report

            summary_bundle = _build_executive_summary_bundle(state)
            audit_report["executive_summary"] = summary_bundle["executive_summary"]

            state_hash = hashlib.sha256(
                json.dumps(state, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]
            audit_report["data_integrity_hash"] = state_hash
            audit_report["generated_at"] = datetime.utcnow().isoformat()

            logger.info("[AuditAgent] Audit report generated with tool-based analysis")

            return {
                **state,
                "audit_report": audit_report,
                "current_step": "audit_complete",
                "audit_entries": [
                    {
                        "agent_type": "audit",
                        "action": "final_report",
                        "detail": {
                            k: v for k, v in audit_report.items() if k != "audit_trail"
                        },
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }
        except Exception as e:
            logger.error(f"[AuditAgent] Failed: {e}", exc_info=True)
            return {
                **state,
                "error": f"AuditAgent failed: {str(e)}",
                "current_step": "audit_failed",
                "audit_entries": [
                    {
                        "agent_type": "audit",
                        "action": "audit_failed",
                        "detail": {"error": str(e)},
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }

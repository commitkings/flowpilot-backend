import json
import logging
import math
from datetime import datetime
from typing import Any

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry

logger = logging.getLogger(__name__)

RISK_SYSTEM_PROMPT = """You are a financial risk scoring engine for FlowPilot.

Your job: score each payout candidate for risk using your tools, then produce a final scored candidate list.

## Your workflow:
1. Use `compute_risk_features` to get statistical risk features for ALL candidates at once
2. Review the features — look at amount deviations, duplicate scores, velocity flags
3. For any candidate that looks suspicious (high z-score, duplicate similarity > 0.7, velocity flag), use `lookup_beneficiary_history` to dig deeper
4. Use `score_candidate` to assign final risk scores one at a time, incorporating all evidence

## Risk scoring criteria:
- **Amount deviation**: Z-score > 2.0 is notable, > 3.0 is high risk
- **Duplicate detection**: Jaccard similarity > 0.7 between beneficiary names + same institution = likely duplicate
- **Velocity**: Multiple payouts to same account in short window = elevated risk
- **Beneficiary history**: New/unknown beneficiaries get slight risk premium
- **Amount vs budget**: Individual payout > 30% of budget cap = elevated risk

## Risk decision thresholds (based on risk_tolerance):
- score <= risk_tolerance: "allow" (safe to auto-approve)
- score <= risk_tolerance + 0.25: "review" (requires human review)
- score > risk_tolerance + 0.25: "block" (auto-reject)

## Final answer format (JSON):
{
  "scored_candidates": [
    {
      "candidate_id": "...",
      "beneficiary_name": "...",
      "institution_code": "...",
      "account_number": "...",
      "amount": 0.0,
      "risk_score": 0.0,
      "risk_reasons": ["reason1"],
      "risk_decision": "allow|review|block",
      "risk_features": {}
    }
  ],
  "risk_summary": {
    "total_scored": 0,
    "allow_count": 0,
    "review_count": 0,
    "block_count": 0,
    "total_amount_at_risk": 0.0,
    "highest_risk_candidate": "...",
    "key_findings": "..."
  }
}
"""


def _compute_jaccard_similarity(s1: str, s2: str) -> float:
    tokens1 = set(s1.lower().split())
    tokens2 = set(s2.lower().split())
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    return len(intersection) / len(union) if union else 0.0


def _build_risk_tools(state: AgentState, db_session=None) -> list[Tool]:
    candidates = state.get("scored_candidates", [])
    risk_tolerance = state.get("risk_tolerance", 0.35)
    ledger = state.get("reconciled_ledger", {})
    budget_cap = state.get("budget_cap")
    transactions = state.get("transactions", [])

    async def compute_risk_features() -> dict[str, Any]:
        if not candidates:
            return {"features": [], "note": "No candidates to analyze"}

        amounts = [c.get("amount", 0.0) for c in candidates]
        mean_amount = sum(amounts) / len(amounts) if amounts else 0.0
        variance = sum((a - mean_amount) ** 2 for a in amounts) / max(len(amounts), 1)
        std_dev = math.sqrt(variance) if variance > 0 else 1.0

        features_list = []
        for i, candidate in enumerate(candidates):
            amount = candidate.get("amount", 0.0)
            z_score = (amount - mean_amount) / std_dev if std_dev > 0 else 0.0
            name = candidate.get("beneficiary_name", "")
            account = candidate.get("account_number", "")
            institution = candidate.get("institution_code", "")

            max_dup_score = 0.0
            dup_match = None
            for j, other in enumerate(candidates):
                if i == j:
                    continue
                other_name = other.get("beneficiary_name", "")
                other_institution = other.get("institution_code", "")
                sim = _compute_jaccard_similarity(name, other_name)
                if other_institution == institution:
                    sim = min(sim * 1.3, 1.0)
                if other.get("account_number") == account and account:
                    sim = 1.0
                if sim > max_dup_score:
                    max_dup_score = sim
                    dup_match = other.get("candidate_id", f"candidate_{j}")

            same_account_in_txns = (
                sum(
                    1
                    for t in transactions
                    if t.get("accountNumber", t.get("recipientAccount")) == account
                )
                if account
                else 0
            )

            budget_ratio = (
                amount / float(budget_cap)
                if budget_cap and float(budget_cap) > 0
                else None
            )

            features = {
                "candidate_id": candidate.get("candidate_id", f"candidate_{i}"),
                "beneficiary_name": name,
                "amount": amount,
                "z_score": round(z_score, 3),
                "amount_deviation": "high"
                if abs(z_score) > 3.0
                else "moderate"
                if abs(z_score) > 2.0
                else "normal",
                "duplicate_similarity": round(max_dup_score, 3),
                "duplicate_match_with": dup_match if max_dup_score > 0.5 else None,
                "is_likely_duplicate": max_dup_score > 0.7,
                "velocity_transactions": same_account_in_txns,
                "velocity_flag": same_account_in_txns > 5,
                "budget_ratio": round(budget_ratio, 3)
                if budget_ratio is not None
                else None,
                "budget_flag": budget_ratio is not None and budget_ratio > 0.3,
            }
            features_list.append(features)

        return {
            "features": features_list,
            "stats": {
                "mean_amount": round(mean_amount, 2),
                "std_deviation": round(std_dev, 2),
                "total_candidates": len(candidates),
                "risk_tolerance": risk_tolerance,
                "budget_cap": float(budget_cap) if budget_cap else None,
            },
        }

    async def lookup_beneficiary_history(candidate_id: str) -> dict[str, Any]:
        candidate = None
        for c in candidates:
            if c.get("candidate_id") == candidate_id:
                candidate = c
                break
        if candidate is None:
            return {"error": f"Candidate {candidate_id} not found"}

        account = candidate.get("account_number", "")
        name = candidate.get("beneficiary_name", "")
        institution = candidate.get("institution_code", "")

        past_txns = (
            [
                t
                for t in transactions
                if t.get("accountNumber", t.get("recipientAccount")) == account
            ]
            if account
            else []
        )

        past_amounts = [t.get("amount", 0.0) for t in past_txns]
        past_statuses = [t.get("status", "") for t in past_txns]

        if db_session is not None:
            try:
                from src.infrastructure.database.repositories.candidate_repository import (
                    CandidateRepository,
                )
                from uuid import UUID

                repo = CandidateRepository(db_session)
                business_id = state.get("business_id")
                if business_id:
                    bid = (
                        UUID(business_id)
                        if isinstance(business_id, str)
                        else business_id
                    )
                    past_candidates, _ = await repo.list_all(business_id=bid, limit=100)

                    same_beneficiary = [
                        pc
                        for pc in past_candidates
                        if getattr(pc, "account_number", None) == account
                    ]
                    return {
                        "candidate_id": candidate_id,
                        "beneficiary_name": name,
                        "account_number": account,
                        "institution_code": institution,
                        "transaction_history": {
                            "total_past_transactions": len(past_txns),
                            "past_amounts": past_amounts[:10],
                            "past_statuses": past_statuses[:10],
                            "avg_past_amount": round(
                                sum(past_amounts) / max(len(past_amounts), 1), 2
                            ),
                        },
                        "payout_history": {
                            "total_past_payouts": len(same_beneficiary),
                            "is_known_beneficiary": len(same_beneficiary) > 0,
                        },
                    }
            except Exception as e:
                logger.warning(f"DB lookup failed for candidate history: {e}")

        return {
            "candidate_id": candidate_id,
            "beneficiary_name": name,
            "account_number": account,
            "institution_code": institution,
            "transaction_history": {
                "total_past_transactions": len(past_txns),
                "past_amounts": past_amounts[:10],
                "past_statuses": past_statuses[:10],
                "avg_past_amount": round(
                    sum(past_amounts) / max(len(past_amounts), 1), 2
                ),
            },
            "payout_history": {
                "note": "No DB session — unable to check past payout records",
                "is_known_beneficiary": len(past_txns) > 0,
            },
        }

    async def compute_amount_deviation(candidate_id: str) -> dict[str, Any]:
        candidate = None
        for c in candidates:
            if c.get("candidate_id") == candidate_id:
                candidate = c
                break
        if candidate is None:
            return {"error": f"Candidate {candidate_id} not found"}

        amount = candidate.get("amount", 0.0)
        all_amounts = [c.get("amount", 0.0) for c in candidates]
        mean_amt = sum(all_amounts) / len(all_amounts) if all_amounts else 0
        variance = sum((a - mean_amt) ** 2 for a in all_amounts) / max(
            len(all_amounts), 1
        )
        std = math.sqrt(variance) if variance > 0 else 1.0
        z = (amount - mean_amt) / std if std > 0 else 0.0

        txn_amounts = [t.get("amount", 0.0) for t in transactions if t.get("amount")]
        txn_mean = sum(txn_amounts) / len(txn_amounts) if txn_amounts else 0
        txn_variance = (
            sum((a - txn_mean) ** 2 for a in txn_amounts) / max(len(txn_amounts), 1)
            if txn_amounts
            else 0
        )
        txn_std = math.sqrt(txn_variance) if txn_variance > 0 else 1.0
        txn_z = (amount - txn_mean) / txn_std if txn_std > 0 and txn_amounts else None

        return {
            "candidate_id": candidate_id,
            "amount": amount,
            "vs_candidates": {
                "mean": round(mean_amt, 2),
                "std_dev": round(std, 2),
                "z_score": round(z, 3),
                "is_outlier": abs(z) > 2.0,
            },
            "vs_transactions": {
                "mean": round(txn_mean, 2),
                "std_dev": round(txn_std, 2),
                "z_score": round(txn_z, 3) if txn_z is not None else None,
                "is_outlier": abs(txn_z) > 2.0 if txn_z is not None else None,
            }
            if txn_amounts
            else {"note": "No transaction history for comparison"},
        }

    async def check_duplicate_candidates(candidate_id: str) -> dict[str, Any]:
        candidate = None
        for c in candidates:
            if c.get("candidate_id") == candidate_id:
                candidate = c
                break
        if candidate is None:
            return {"error": f"Candidate {candidate_id} not found"}

        name = candidate.get("beneficiary_name", "")
        account = candidate.get("account_number", "")
        institution = candidate.get("institution_code", "")

        duplicates = []
        for other in candidates:
            if other.get("candidate_id") == candidate_id:
                continue
            sim = _compute_jaccard_similarity(name, other.get("beneficiary_name", ""))
            same_account = other.get("account_number") == account and account
            same_institution = other.get("institution_code") == institution

            if sim > 0.5 or same_account:
                duplicates.append(
                    {
                        "other_candidate_id": other.get("candidate_id"),
                        "other_name": other.get("beneficiary_name"),
                        "name_similarity": round(sim, 3),
                        "same_account": bool(same_account),
                        "same_institution": same_institution,
                        "other_amount": other.get("amount"),
                        "is_likely_duplicate": sim > 0.7 or bool(same_account),
                    }
                )

        return {
            "candidate_id": candidate_id,
            "beneficiary_name": name,
            "potential_duplicates": duplicates,
            "has_duplicates": any(d["is_likely_duplicate"] for d in duplicates),
        }

    async def score_candidate(
        candidate_id: str,
        risk_score: float,
        risk_reasons: str,
    ) -> dict[str, Any]:
        score = max(0.0, min(1.0, risk_score))
        reasons = [r.strip() for r in risk_reasons.split("|") if r.strip()]

        if score <= risk_tolerance:
            decision = "allow"
        elif score <= risk_tolerance + 0.25:
            decision = "review"
        else:
            decision = "block"

        for c in candidates:
            if c.get("candidate_id") == candidate_id:
                c["risk_score"] = score
                c["risk_reasons"] = reasons
                c["risk_decision"] = decision
                break

        return {
            "candidate_id": candidate_id,
            "risk_score": score,
            "risk_decision": decision,
            "risk_reasons": reasons,
            "thresholds": {
                "allow_max": risk_tolerance,
                "review_max": risk_tolerance + 0.25,
                "block_above": risk_tolerance + 0.25,
            },
        }

    return [
        Tool(
            name="compute_risk_features",
            description="Compute statistical risk features for ALL candidates: z-score amount deviation, duplicate similarity, velocity flags, budget ratio.",
            parameters=[],
            execute=compute_risk_features,
        ),
        Tool(
            name="lookup_beneficiary_history",
            description="Look up past transaction and payout history for a specific candidate's beneficiary account.",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to investigate",
                ),
            ],
            execute=lookup_beneficiary_history,
        ),
        Tool(
            name="compute_amount_deviation",
            description="Compute detailed amount deviation analysis for a specific candidate vs other candidates and vs historical transactions.",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to analyze",
                ),
            ],
            execute=compute_amount_deviation,
        ),
        Tool(
            name="check_duplicate_candidates",
            description="Check if a specific candidate has potential duplicates among other candidates (name similarity, same account).",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to check for duplicates",
                ),
            ],
            execute=check_duplicate_candidates,
        ),
        Tool(
            name="score_candidate",
            description="Assign a final risk score and decision to a candidate. Call this for EACH candidate after analysis.",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to score",
                ),
                ToolParam(
                    name="risk_score",
                    param_type=ToolParamType.NUMBER,
                    description="Risk score from 0.0 (safe) to 1.0 (dangerous)",
                ),
                ToolParam(
                    name="risk_reasons",
                    param_type=ToolParamType.STRING,
                    description="Pipe-separated risk reasons, e.g. 'high amount deviation|new beneficiary'",
                ),
            ],
            execute=score_candidate,
        ),
    ]


class RiskAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("RiskAgent")

    async def run(self, state: AgentState, db_session=None) -> AgentState:
        candidates = state.get("scored_candidates", [])
        risk_tolerance = state.get("risk_tolerance", 0.35)
        ledger = state.get("reconciled_ledger", {})

        logger.info(
            f"[RiskAgent] Scoring {len(candidates)} candidates (tolerance: {risk_tolerance})"
        )

        if not candidates:
            logger.warning("[RiskAgent] No candidates to score")
            return {
                **state,
                "current_step": "risk_complete",
                "audit_entries": [
                    {
                        "agent_type": "risk",
                        "action": "risk_skipped",
                        "detail": {"reason": "no candidates"},
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }

        self.registry = ToolRegistry()
        for tool in _build_risk_tools(state, db_session):
            self.registry.register(tool)

        candidate_summary = json.dumps(
            [
                {
                    "candidate_id": c.get("candidate_id", f"c_{i}"),
                    "beneficiary_name": c.get("beneficiary_name", ""),
                    "institution_code": c.get("institution_code", ""),
                    "account_number": c.get("account_number", ""),
                    "amount": c.get("amount", 0),
                }
                for i, c in enumerate(candidates)
            ],
            indent=2,
        )

        user_prompt = f"""Score the following {len(candidates)} payout candidates for risk.

Risk tolerance: {risk_tolerance}
Budget cap: {state.get("budget_cap", "No limit")}

Candidates:
{candidate_summary}

Reconciled ledger context:
{json.dumps(ledger, indent=2)}

Steps:
1. First call compute_risk_features to get statistical features for all candidates
2. Review the features — investigate any flagged candidates with lookup_beneficiary_history or check_duplicate_candidates
3. Call score_candidate for EACH candidate with your determined risk_score and reasons
4. Produce your final JSON with all scored candidates and a risk summary"""

        try:
            await self.emit_progress(
                f"Scoring {len(candidates)} candidates...",
                {
                    "candidate_count": len(candidates),
                    "risk_tolerance": risk_tolerance,
                },
            )

            response = await self.reason_and_act_json(
                system_prompt=RISK_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

            try:
                result = json.loads(response)
            except json.JSONDecodeError:
                result = {}

            for c in candidates:
                c.setdefault("risk_score", 0.5)
                c.setdefault("risk_reasons", ["scoring incomplete"])
                score = c.get("risk_score", 0.5)
                if "risk_decision" not in c:
                    if score <= risk_tolerance:
                        c["risk_decision"] = "allow"
                    elif score <= risk_tolerance + 0.25:
                        c["risk_decision"] = "review"
                    else:
                        c["risk_decision"] = "block"

            allow_count = sum(
                1 for c in candidates if c.get("risk_decision") == "allow"
            )
            review_count = sum(
                1 for c in candidates if c.get("risk_decision") == "review"
            )
            block_count = sum(
                1 for c in candidates if c.get("risk_decision") == "block"
            )

            logger.info(
                f"[RiskAgent] Results: allow={allow_count}, review={review_count}, block={block_count}"
            )

            return {
                **state,
                "scored_candidates": candidates,
                "current_step": "risk_complete",
                "audit_entries": [
                    {
                        "agent_type": "risk",
                        "action": "risk_scoring_complete",
                        "detail": {
                            "total_scored": len(candidates),
                            "allow": allow_count,
                            "review": review_count,
                            "block": block_count,
                            "risk_summary": result.get("risk_summary", {}),
                        },
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }
        except Exception as e:
            logger.error(f"[RiskAgent] Failed: {e}", exc_info=True)
            return {
                **state,
                "error": f"RiskAgent failed: {str(e)}",
                "current_step": "risk_failed",
                "audit_entries": [
                    {
                        "agent_type": "risk",
                        "action": "risk_failed",
                        "detail": {"error": str(e)},
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            }

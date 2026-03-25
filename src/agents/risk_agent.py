"""
Real Risk Intelligence with Feature Engineering

This agent performs genuine risk analysis using:
1. Feature engineering (velocity, duplicates, amount deviation, etc.)
2. A weighted scoring model with business-configurable weights
3. LLM review of computed scores (not LLM-guessed scores)
4. Hard guardrails that cannot be overridden
5. Persisted risk features for explainability
"""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.agents.base import BaseAgent
from src.agents.state import AgentState
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

# ============================================================================
# Default Risk Weights (can be overridden via business_config.preferences)
# ============================================================================

DEFAULT_RISK_WEIGHTS: dict[str, float] = {
    "amount_z_score": 0.20,  # How unusual is this amount?
    "is_new_beneficiary": 0.15,  # First-time recipient
    "duplicate_similarity": 0.15,  # Possible double-payment
    "velocity_7d": 0.10,  # Rapid repeated payments (7 days)
    "velocity_30d": 0.05,  # Monthly velocity
    "amount_vs_cap": 0.10,  # How close to budget limit
    "round_number_bias": 0.05,  # Suspiciously round amount
    "name_inconsistency": 0.10,  # Name changed for same account
    "days_since_last_payout": 0.05,  # Recency factor (inverse)
    "account_age_factor": 0.05,  # Account maturity (inverse for new)
}

DEFAULT_RISK_THRESHOLDS: dict[str, float] = {
    "allow_max": 0.30,  # score <= this → allow
    "review_max": 0.60,  # score <= this → review
    # score > review_max → block
}

# ============================================================================
# System Prompt
# ============================================================================

RISK_SYSTEM_PROMPT = """You are a financial risk analyst reviewing pre-computed risk features for payout candidates.

## Your Workflow:
1. Call `get_risk_thresholds` to understand this business's risk configuration and weights
2. Optionally call `search_similar_run_memories` with keywords from the run objective (e.g. payroll, December) to see how similar past runs performed
3. Call `compute_risk_features` to get statistical features for ALL candidates at once
4. For candidates with elevated risk signals (high z-score, duplicate flags, velocity flags):
   - Call `compute_velocity_features` to get 7-day and 30-day payout history
   - Call `cross_reference_beneficiaries` if name patterns look suspicious
   - Call `detect_round_number_bias` for suspiciously round amounts
5. Call `compute_weighted_risk_score` for each candidate to get the computed weighted score
6. Review the computed score and features. You may adjust the score by ±0.1 with written justification.
7. Call `score_candidate` to finalize each candidate's risk assessment

## Guardrails (you CANNOT override these — they are enforced by the system):
- If amount > budget_cap: decision MUST be "block"
- If duplicate_similarity > 0.95 (near-exact duplicate): decision MUST be "review" or "block"
- If is_new_beneficiary AND amount_z_score > 3.0: decision MUST be "review" minimum

## Scoring Model:
The weighted score is computed from features using business-specific weights.
Each feature is normalized to 0-1 and multiplied by its weight.
Your role is to REVIEW and EXPLAIN the score, not to guess a score yourself.

You may adjust the final score by ±0.1 if you identify factors the model missed, but you MUST provide written justification in risk_reasons.

## Risk Criteria Explained:
- **amount_z_score**: Z-score > 2.0 is notable, > 3.0 is high risk (unusually large/small amount)
- **is_new_beneficiary**: First-time payment to this account — slightly elevated risk
- **duplicate_similarity**: Jaccard similarity > 0.7 = likely duplicate, > 0.95 = near-certain
- **velocity_7d / velocity_30d**: Multiple payouts to same account in short window
- **amount_vs_cap**: Individual payout > 30% of budget cap = elevated risk
- **round_number_bias**: Amounts exactly divisible by 10000/50000/100000 with no kobo
- **name_inconsistency**: Different names used for same account historically
- **days_since_last_payout**: Very recent payout to same account (< 7 days)
- **account_age_factor**: New accounts (first seen < 30 days ago) get slight premium

## Memory-Aware Risk Assessment (Phase 7):
For each candidate, call `get_beneficiary_reputation` to check their historical payout reputation.
Factor the reputation score into your risk assessment:
- Beneficiaries with success_rate < 0.5 should receive elevated risk scores (+0.1 to +0.2)
- Beneficiaries with failed_payouts > 2 and last_failure_reason containing "verification" are high risk
- New beneficiaries (not found in memory) should be flagged for extra verification
- Consider last_failure_reason when similar patterns might recur
- Beneficiaries with reputation_score > 0.8 and successful_payouts > 5 can receive slight risk reduction (-0.05)

## Final Answer Format (JSON):
{
  "scored_candidates": [
    {
      "candidate_id": "...",
      "beneficiary_name": "...",
      "institution_code": "...",
      "account_number": "...",
      "amount": 0.0,
      "risk_score": 0.0,
      "risk_reasons": ["reason1", "reason2"],
      "risk_decision": "allow|review|block",
      "computed_score": 0.0,
      "adjustment": 0.0,
      "adjustment_reason": "..."
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


# ============================================================================
# Helper Functions
# ============================================================================


def _compute_jaccard_similarity(s1: str, s2: str) -> float:
    """Compute Jaccard similarity between two strings (tokenized)."""
    tokens1 = set(s1.lower().split())
    tokens2 = set(s2.lower().split())
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    return len(intersection) / len(union) if union else 0.0


def _normalize_z_score(z: float) -> float:
    """Normalize z-score to 0-1 range using sigmoid-like mapping."""
    # z=0 → 0.0, z=2 → 0.5, z=4 → 0.88, z=6 → 0.95
    if z <= 0:
        return 0.0
    return min(1.0, 1 - (1 / (1 + z / 2)))


def _normalize_velocity(count: int, max_count: int = 10) -> float:
    """Normalize velocity count to 0-1 range."""
    if count <= 0:
        return 0.0
    return min(1.0, count / max_count)


def _normalize_days_since(days: int | None, threshold: int = 30) -> float:
    """
    Normalize days-since-last-payout to risk factor.
    More recent = higher risk, None (new) = medium risk.
    """
    if days is None:
        return 0.5  # New beneficiary gets medium score here
    if days <= 1:
        return 1.0  # Same day or yesterday = high risk
    if days <= 7:
        return 0.7  # Within a week
    if days <= 30:
        return 0.3  # Within a month
    return 0.0  # Over a month ago = low risk


def _normalize_account_age(days: int | None, threshold: int = 90) -> float:
    """
    Normalize account age to risk factor.
    Newer accounts = higher risk, None = highest risk (brand new).
    """
    if days is None:
        return 1.0  # Brand new account
    if days <= 7:
        return 0.8
    if days <= 30:
        return 0.5
    if days <= 90:
        return 0.2
    return 0.0  # Well-established account


def _is_round_number(amount: float) -> tuple[bool, str, float]:
    """
    Check if amount is suspiciously round.
    Returns: (is_round, pattern, risk_factor)
    """
    # Check for kobo (decimal part)
    if amount != int(amount):
        return False, "none", 0.0

    amount_int = int(amount)

    # Check divisibility patterns (in order of suspicion)
    if amount_int >= 100000 and amount_int % 100000 == 0:
        return True, "100k", 0.8
    if amount_int >= 50000 and amount_int % 50000 == 0:
        return True, "50k", 0.6
    if amount_int >= 10000 and amount_int % 10000 == 0:
        return True, "10k", 0.4

    return False, "none", 0.0


def _compute_weighted_score(
    features: dict[str, float], weights: dict[str, float]
) -> float:
    """
    Compute weighted risk score from normalized features.

    Args:
        features: Dict of feature_name → normalized value (0-1)
        weights: Dict of feature_name → weight (should sum to 1.0)

    Returns:
        Weighted score clamped to 0-1
    """
    score = 0.0
    for feature_name, weight in weights.items():
        feature_value = features.get(feature_name, 0.0)
        score += feature_value * weight

    return max(0.0, min(1.0, score))


def _apply_guardrails(
    score: float,
    decision: str,
    features: dict,
    budget_cap: float | None,
    amount: float,
) -> tuple[float, str, list[str]]:
    """
    Apply non-negotiable guardrails to risk decision.

    Returns: (final_score, final_decision, guardrail_reasons)
    """
    guardrail_reasons = []

    # Guardrail 1: Amount exceeds budget cap → block
    if budget_cap and amount > budget_cap:
        guardrail_reasons.append(
            f"GUARDRAIL: Amount {amount:,.2f} exceeds budget cap {budget_cap:,.2f}"
        )
        return max(score, 0.9), "block", guardrail_reasons

    # Guardrail 2: Near-exact duplicate → review minimum
    dup_score = features.get("duplicate_similarity", 0.0)
    if dup_score > 0.95:
        guardrail_reasons.append(
            f"GUARDRAIL: Near-exact duplicate detected (similarity={dup_score:.2f})"
        )
        if decision == "allow":
            decision = "review"
            score = max(score, 0.5)

    # Guardrail 3: New beneficiary with high amount deviation → review minimum
    is_new = features.get("is_new_beneficiary", False)
    z_score = features.get("amount_z_score_raw", 0.0)
    if is_new and z_score > 3.0:
        guardrail_reasons.append(
            f"GUARDRAIL: New beneficiary with high amount deviation (z={z_score:.2f})"
        )
        if decision == "allow":
            decision = "review"
            score = max(score, 0.4)

    return score, decision, guardrail_reasons


# ============================================================================
# Tool Builder
# ============================================================================


def _build_risk_tools(state: AgentState, db_session=None) -> list[Tool]:
    """Build the risk analysis tools with state and DB access."""

    candidates = state.get("scored_candidates", [])
    risk_tolerance = state.get("risk_tolerance", 0.35)
    budget_cap = state.get("budget_cap")
    transactions = state.get("transactions", [])
    business_id = state.get("business_id")
    run_id = state.get("run_id")

    # Storage for computed features (shared across tools)
    computed_features: dict[str, dict] = {}

    # Get risk config from business preferences or use defaults
    business_config = state.get("business_config", {})
    preferences = business_config.get("preferences", {}) if business_config else {}
    risk_weights = preferences.get("risk_weights", DEFAULT_RISK_WEIGHTS)
    risk_thresholds = preferences.get("risk_thresholds", DEFAULT_RISK_THRESHOLDS)

    # ─── Tool 1: get_risk_thresholds ───────────────────────────────────────

    async def get_risk_thresholds() -> dict[str, Any]:
        """Get business-specific risk weights and thresholds from config."""
        return {
            "weights": risk_weights,
            "thresholds": {
                "allow_max": risk_thresholds.get("allow_max", 0.30),
                "review_max": risk_thresholds.get("review_max", 0.60),
            },
            "risk_tolerance": risk_tolerance,
            "budget_cap": float(budget_cap) if budget_cap else None,
            "guardrails": {
                "block_if_over_cap": True,
                "review_new_high_amount": True,
                "review_near_duplicates": True,
            },
            "note": "Weights should sum to ~1.0. Thresholds define decision boundaries.",
        }

    # ─── Tool 2: compute_risk_features (enhanced) ──────────────────────────

    async def compute_risk_features() -> dict[str, Any]:
        """Compute statistical risk features for ALL candidates at once."""
        if not candidates:
            return {"features": [], "note": "No candidates to analyze"}

        # Compute batch-level statistics
        amounts = [c.get("amount", 0.0) for c in candidates]
        mean_amount = sum(amounts) / len(amounts) if amounts else 0.0
        variance = sum((a - mean_amount) ** 2 for a in amounts) / max(len(amounts), 1)
        std_dev = math.sqrt(variance) if variance > 0 else 1.0

        features_list = []

        for i, candidate in enumerate(candidates):
            cid = candidate.get("candidate_id", f"candidate_{i}")
            amount = candidate.get("amount", 0.0)
            name = candidate.get("beneficiary_name", "")
            account = candidate.get("account_number", "")
            institution = candidate.get("institution_code", "")

            # Amount z-score
            z_score = (amount - mean_amount) / std_dev if std_dev > 0 else 0.0

            # Duplicate detection within batch
            max_dup_score = 0.0
            dup_match = None
            for j, other in enumerate(candidates):
                if i == j:
                    continue
                other_name = other.get("beneficiary_name", "")
                other_institution = other.get("institution_code", "")
                sim = _compute_jaccard_similarity(name, other_name)
                # Boost similarity if same institution
                if other_institution == institution:
                    sim = min(sim * 1.3, 1.0)
                # Exact account match = definite duplicate
                if other.get("account_number") == account and account:
                    sim = 1.0
                if sim > max_dup_score:
                    max_dup_score = sim
                    dup_match = other.get("candidate_id", f"candidate_{j}")

            # Velocity from current transactions (basic)
            same_account_in_txns = (
                sum(
                    1
                    for t in transactions
                    if t.get("accountNumber", t.get("recipientAccount")) == account
                )
                if account
                else 0
            )

            # Budget ratio
            budget_ratio = (
                amount / float(budget_cap)
                if budget_cap and float(budget_cap) > 0
                else None
            )

            # Round number check
            is_round, round_pattern, round_risk = _is_round_number(amount)

            # Check if new beneficiary (will be refined by velocity tool)
            is_new_beneficiary = same_account_in_txns == 0

            features = {
                "candidate_id": cid,
                "beneficiary_name": name,
                "account_number": account,
                "institution_code": institution,
                "amount": amount,
                # Raw values for display
                "z_score": round(z_score, 3),
                "amount_deviation": (
                    "high"
                    if abs(z_score) > 3.0
                    else "moderate"
                    if abs(z_score) > 2.0
                    else "normal"
                ),
                "duplicate_similarity": round(max_dup_score, 3),
                "duplicate_match_with": dup_match if max_dup_score > 0.5 else None,
                "is_likely_duplicate": max_dup_score > 0.7,
                "velocity_in_batch_txns": same_account_in_txns,
                "budget_ratio": round(budget_ratio, 3)
                if budget_ratio is not None
                else None,
                "budget_flag": budget_ratio is not None and budget_ratio > 0.3,
                "is_round_number": is_round,
                "round_pattern": round_pattern,
                "is_new_beneficiary": is_new_beneficiary,
                # Normalized values for scoring
                "normalized": {
                    "amount_z_score": round(_normalize_z_score(abs(z_score)), 3),
                    "duplicate_similarity": round(max_dup_score, 3),
                    "amount_vs_cap": round(budget_ratio, 3) if budget_ratio else 0.0,
                    "round_number_bias": round_risk,
                    "is_new_beneficiary": 1.0 if is_new_beneficiary else 0.0,
                },
            }

            # Store for later use
            computed_features[cid] = features
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
            "note": "Use compute_velocity_features for historical 7d/30d velocity data.",
        }

    # ─── Tool 3: compute_velocity_features ─────────────────────────────────

    async def compute_velocity_features(candidate_id: str) -> dict[str, Any]:
        """Query 7-day and 30-day payout velocity for a candidate's account."""
        candidate = None
        for c in candidates:
            if c.get("candidate_id") == candidate_id:
                candidate = c
                break

        if candidate is None:
            return {"error": f"Candidate {candidate_id} not found"}

        account = candidate.get("account_number", "")
        result = {
            "candidate_id": candidate_id,
            "account_number": account,
            "velocity_7d": 0,
            "velocity_30d": 0,
            "avg_historical_amount": None,
            "days_since_last_payout": None,
            "account_age_days": None,
            "is_new_beneficiary": True,
        }

        if not db_session or not business_id or not account:
            result["note"] = (
                "No DB session or business_id — using transaction-only analysis"
            )
            # Fallback to transaction data
            same_account_txns = [
                t
                for t in transactions
                if t.get("accountNumber", t.get("recipientAccount")) == account
            ]
            result["velocity_30d"] = len(same_account_txns)
            result["is_new_beneficiary"] = len(same_account_txns) == 0
            return result

        try:
            from src.infrastructure.database.repositories.candidate_repository import (
                CandidateRepository,
            )

            repo = CandidateRepository(db_session)
            bid = UUID(business_id) if isinstance(business_id, str) else business_id
            rid = UUID(run_id) if isinstance(run_id, str) else run_id

            # 7-day velocity
            velocity_7d = await repo.count_payouts_to_account(
                business_id=bid,
                account_number=account,
                days=7,
                exclude_run_id=rid,
            )

            # 30-day velocity
            velocity_30d = await repo.count_payouts_to_account(
                business_id=bid,
                account_number=account,
                days=30,
                exclude_run_id=rid,
            )

            # Average historical amount
            avg_amount = await repo.get_average_amount_for_account(
                business_id=bid,
                account_number=account,
                exclude_run_id=rid,
            )

            # Days since last payout
            last_payout = await repo.get_last_payout_date_for_account(
                business_id=bid,
                account_number=account,
                exclude_run_id=rid,
            )
            days_since = None
            if last_payout:
                days_since = (_utc_now() - last_payout).days

            # Account age
            first_payout = await repo.get_first_payout_date_for_account(
                business_id=bid,
                account_number=account,
            )
            account_age = None
            if first_payout:
                account_age = (_utc_now() - first_payout).days

            is_new = velocity_30d == 0 and account_age is None

            result.update(
                {
                    "velocity_7d": velocity_7d,
                    "velocity_30d": velocity_30d,
                    "avg_historical_amount": float(avg_amount) if avg_amount else None,
                    "days_since_last_payout": days_since,
                    "account_age_days": account_age,
                    "is_new_beneficiary": is_new,
                    "normalized": {
                        "velocity_7d": _normalize_velocity(velocity_7d, max_count=5),
                        "velocity_30d": _normalize_velocity(velocity_30d, max_count=10),
                        "days_since_last_payout": _normalize_days_since(days_since),
                        "account_age_factor": _normalize_account_age(account_age),
                    },
                }
            )

            # Update computed features
            if candidate_id in computed_features:
                computed_features[candidate_id]["velocity_7d"] = velocity_7d
                computed_features[candidate_id]["velocity_30d"] = velocity_30d
                computed_features[candidate_id]["is_new_beneficiary"] = is_new
                computed_features[candidate_id]["days_since_last_payout"] = days_since
                computed_features[candidate_id]["account_age_days"] = account_age
                computed_features[candidate_id]["normalized"].update(
                    result["normalized"]
                )

        except Exception as e:
            logger.warning(f"DB velocity lookup failed: {e}")
            result["error"] = str(e)

        return result

    # ─── Tool 4: detect_round_number_bias ──────────────────────────────────

    async def detect_round_number_bias(candidate_id: str) -> dict[str, Any]:
        """Check if amount is suspiciously round (divisible by 10k/50k/100k with no kobo)."""
        candidate = None
        for c in candidates:
            if c.get("candidate_id") == candidate_id:
                candidate = c
                break

        if candidate is None:
            return {"error": f"Candidate {candidate_id} not found"}

        amount = candidate.get("amount", 0.0)
        is_round, pattern, risk_factor = _is_round_number(amount)

        return {
            "candidate_id": candidate_id,
            "amount": amount,
            "is_round_number": is_round,
            "round_pattern": pattern,
            "risk_factor": risk_factor,
            "explanation": (
                f"Amount {amount:,.2f} is {'suspiciously' if is_round else 'not'} round"
                + (f" (divisible by {pattern})" if is_round else "")
            ),
        }

    # ─── Tool 5: cross_reference_beneficiaries ─────────────────────────────

    async def cross_reference_beneficiaries(candidate_id: str) -> dict[str, Any]:
        """Find same-account-different-name patterns in historical data."""
        candidate = None
        for c in candidates:
            if c.get("candidate_id") == candidate_id:
                candidate = c
                break

        if candidate is None:
            return {"error": f"Candidate {candidate_id} not found"}

        account = candidate.get("account_number", "")
        current_name = candidate.get("beneficiary_name", "")

        result = {
            "candidate_id": candidate_id,
            "account_number": account,
            "current_name": current_name,
            "name_variations": [],
            "name_consistency_score": 1.0,
            "is_name_suspicious": False,
        }

        if not db_session or not business_id or not account:
            result["note"] = "No DB session — cannot check historical names"
            return result

        try:
            from src.infrastructure.database.repositories.candidate_repository import (
                CandidateRepository,
            )

            repo = CandidateRepository(db_session)
            bid = UUID(business_id) if isinstance(business_id, str) else business_id
            rid = UUID(run_id) if isinstance(run_id, str) else run_id

            # Get all names used for this account
            name_variations = await repo.get_name_variations_for_account(
                business_id=bid,
                account_number=account,
                exclude_run_id=rid,
            )

            if not name_variations:
                result["note"] = "No historical data for this account"
                return result

            # Calculate name consistency
            max_similarity = 0.0
            min_similarity = 1.0
            for past_name in name_variations:
                sim = _compute_jaccard_similarity(current_name, past_name)
                max_similarity = max(max_similarity, sim)
                min_similarity = min(min_similarity, sim)

            # If there are very different names, flag it
            is_suspicious = min_similarity < 0.5 and len(name_variations) > 1

            result.update(
                {
                    "name_variations": name_variations[:5],  # Limit to 5
                    "name_consistency_score": round(min_similarity, 3),
                    "is_name_suspicious": is_suspicious,
                    "normalized": {
                        "name_inconsistency": round(1.0 - min_similarity, 3),
                    },
                }
            )

            # Update computed features
            if candidate_id in computed_features:
                computed_features[candidate_id]["name_variations"] = name_variations[:5]
                computed_features[candidate_id]["name_consistency_score"] = (
                    min_similarity
                )
                computed_features[candidate_id]["normalized"]["name_inconsistency"] = (
                    1.0 - min_similarity
                )

        except Exception as e:
            logger.warning(f"DB name lookup failed: {e}")
            result["error"] = str(e)

        return result

    # ─── Tool 6: compute_weighted_risk_score ───────────────────────────────

    async def compute_weighted_risk_score(candidate_id: str) -> dict[str, Any]:
        """Apply weighted scoring model to computed features."""
        if candidate_id not in computed_features:
            return {
                "error": f"No features computed for {candidate_id}. Call compute_risk_features first."
            }

        features = computed_features[candidate_id]
        normalized = features.get("normalized", {})

        # Ensure all features have values
        feature_values = {
            "amount_z_score": normalized.get("amount_z_score", 0.0),
            "is_new_beneficiary": 1.0
            if features.get("is_new_beneficiary", False)
            else 0.0,
            "duplicate_similarity": normalized.get("duplicate_similarity", 0.0),
            "velocity_7d": normalized.get("velocity_7d", 0.0),
            "velocity_30d": normalized.get("velocity_30d", 0.0),
            "amount_vs_cap": normalized.get("amount_vs_cap", 0.0),
            "round_number_bias": normalized.get("round_number_bias", 0.0),
            "name_inconsistency": normalized.get("name_inconsistency", 0.0),
            "days_since_last_payout": normalized.get("days_since_last_payout", 0.0),
            "account_age_factor": normalized.get(
                "account_age_factor", 0.5
            ),  # Default medium for unknown
        }

        # Compute weighted score
        weighted_score = _compute_weighted_score(feature_values, risk_weights)

        # Determine suggested decision based on thresholds
        allow_max = risk_thresholds.get("allow_max", 0.30)
        review_max = risk_thresholds.get("review_max", 0.60)

        if weighted_score <= allow_max:
            suggested_decision = "allow"
        elif weighted_score <= review_max:
            suggested_decision = "review"
        else:
            suggested_decision = "block"

        # Compute feature contributions for explainability
        contributions = {}
        for feature_name, weight in risk_weights.items():
            value = feature_values.get(feature_name, 0.0)
            contribution = value * weight
            if contribution > 0.01:  # Only show non-trivial contributions
                contributions[feature_name] = {
                    "value": round(value, 3),
                    "weight": weight,
                    "contribution": round(contribution, 4),
                }

        return {
            "candidate_id": candidate_id,
            "weighted_score": round(weighted_score, 4),
            "suggested_decision": suggested_decision,
            "thresholds": {
                "allow_max": allow_max,
                "review_max": review_max,
            },
            "feature_contributions": contributions,
            "top_risk_factors": sorted(
                contributions.keys(),
                key=lambda k: contributions[k]["contribution"],
                reverse=True,
            )[:3],
        }

    # ─── Tool 7: score_candidate (finalize) ────────────────────────────────

    async def score_candidate(
        candidate_id: str,
        risk_score: float,
        risk_decision: str,
        risk_reasons: str,
        adjustment: float = 0.0,
        adjustment_reason: str = "",
    ) -> dict[str, Any]:
        """
        Finalize risk score and decision for a candidate.

        Args:
            candidate_id: The candidate to score
            risk_score: Final risk score (0.0-1.0), typically from compute_weighted_risk_score
            risk_decision: Decision (allow/review/block)
            risk_reasons: Pipe-separated risk reasons
            adjustment: Your adjustment to the computed score (±0.1 max)
            adjustment_reason: Justification for adjustment (required if adjustment != 0)
        """
        # Clamp adjustment
        adjustment = max(-0.1, min(0.1, adjustment))

        # Apply adjustment
        final_score = max(0.0, min(1.0, risk_score + adjustment))

        # Parse reasons
        reasons = [r.strip() for r in risk_reasons.split("|") if r.strip()]

        # Add adjustment reason if applicable
        if adjustment != 0 and adjustment_reason:
            reasons.append(
                f"Manual adjustment ({adjustment:+.2f}): {adjustment_reason}"
            )

        # Get features for guardrail check
        features = computed_features.get(candidate_id, {})
        amount = 0.0
        for c in candidates:
            if c.get("candidate_id") == candidate_id:
                amount = c.get("amount", 0.0)
                break

        # Prepare feature dict for guardrails
        guardrail_features = {
            "duplicate_similarity": features.get("normalized", {}).get(
                "duplicate_similarity", 0.0
            ),
            "is_new_beneficiary": features.get("is_new_beneficiary", False),
            "amount_z_score_raw": features.get("z_score", 0.0),
        }

        # Apply guardrails
        final_score, final_decision, guardrail_reasons = _apply_guardrails(
            final_score,
            risk_decision,
            guardrail_features,
            float(budget_cap) if budget_cap else None,
            amount,
        )

        # Add guardrail reasons
        reasons.extend(guardrail_reasons)

        # Update candidate in state
        for c in candidates:
            if c.get("candidate_id") == candidate_id:
                c["risk_score"] = final_score
                c["risk_reasons"] = reasons
                c["risk_decision"] = final_decision
                c["risk_features"] = features.get("normalized", {})
                break

        return {
            "candidate_id": candidate_id,
            "risk_score": round(final_score, 4),
            "risk_decision": final_decision,
            "risk_reasons": reasons,
            "original_score": round(risk_score, 4),
            "adjustment_applied": round(adjustment, 4),
            "guardrails_triggered": len(guardrail_reasons) > 0,
            "guardrail_reasons": guardrail_reasons,
        }

    # ─── Tool 8: lookup_beneficiary_history (enhanced) ─────────────────────

    async def lookup_beneficiary_history(candidate_id: str) -> dict[str, Any]:
        """Look up past transaction and payout history for a candidate's beneficiary."""
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

        # Transaction history
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

        result = {
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
        }

        if not db_session or not business_id:
            result["payout_history"] = {
                "note": "No DB session — cannot check past payout records",
                "is_known_beneficiary": len(past_txns) > 0,
            }
            return result

        try:
            from src.infrastructure.database.repositories.candidate_repository import (
                CandidateRepository,
            )

            repo = CandidateRepository(db_session)
            bid = UUID(business_id) if isinstance(business_id, str) else business_id
            rid = UUID(run_id) if isinstance(run_id, str) else run_id

            # Get historical payouts
            past_payouts = await repo.get_historical_payouts_to_account(
                business_id=bid,
                account_number=account,
                days=90,
                exclude_run_id=rid,
            )

            payout_amounts = [float(p.amount) for p in past_payouts]
            payout_statuses = [p.execution_status for p in past_payouts]

            result["payout_history"] = {
                "total_past_payouts": len(past_payouts),
                "is_known_beneficiary": len(past_payouts) > 0,
                "past_payout_amounts": payout_amounts[:10],
                "past_execution_statuses": payout_statuses[:10],
                "avg_payout_amount": (
                    round(sum(payout_amounts) / len(payout_amounts), 2)
                    if payout_amounts
                    else None
                ),
                "success_rate": (
                    round(
                        sum(1 for s in payout_statuses if s == "success")
                        / len(payout_statuses),
                        2,
                    )
                    if payout_statuses
                    else None
                ),
            }

        except Exception as e:
            logger.warning(f"DB history lookup failed: {e}")
            result["payout_history"] = {"error": str(e)}

        return result

    # ─── Tool 9: get_beneficiary_reputation (Phase 7 Memory) ───────────────

    async def get_beneficiary_reputation(account_number: str, bank_code: str) -> dict[str, Any]:
        """
        Query historical reputation for a beneficiary from the memory system.
        
        This queries the beneficiary_reputation table which aggregates outcomes
        across all past payout attempts to this account, providing:
        - Success rate and payout counts
        - Reputation score (0-1 Bayesian estimate)
        - Last outcome and failure reason
        - Average payout amount
        
        Use this to factor historical performance into risk scoring.
        """
        if not db_session:
            return {
                "found": False,
                "account_number": account_number,
                "bank_code": bank_code,
                "error": "No database session available",
            }

        try:
            from src.infrastructure.database.repositories.beneficiary_reputation_repository import (
                BeneficiaryReputationRepository,
            )

            repo = BeneficiaryReputationRepository(db_session)
            rep = await repo.get_reputation(account_number, bank_code)

            if not rep:
                return {
                    "found": False,
                    "account_number": account_number,
                    "bank_code": bank_code,
                    "message": "No historical data for this beneficiary",
                }

            return {
                "found": True,
                "account_number": account_number,
                "bank_code": bank_code,
                "beneficiary_name": rep.beneficiary_name,
                "total_attempts": rep.total_attempts,
                "successful_payouts": rep.successful_payouts,
                "failed_payouts": rep.failed_payouts,
                "success_rate": float(rep.success_rate) if rep.success_rate else 0.0,
                "reputation_score": float(rep.reputation_score) if rep.reputation_score else 0.5,
                "last_outcome": rep.last_outcome,
                "last_failure_reason": rep.last_failure_reason,
                "last_payout_at": rep.last_payout_at.isoformat() if rep.last_payout_at else None,
                "total_amount_paid": float(rep.total_amount_paid) if rep.total_amount_paid else 0.0,
                "average_amount": float(rep.average_amount) if rep.average_amount else None,
                "first_seen_at": rep.first_seen_at.isoformat() if rep.first_seen_at else None,
            }

        except Exception as e:
            logger.warning(f"Reputation lookup failed for {account_number}: {e}")
            return {
                "found": False,
                "account_number": account_number,
                "bank_code": bank_code,
                "error": str(e),
            }

    async def search_similar_run_memories(search_query: str) -> dict[str, Any]:
        if db_session is None:
            return {"error": "No database session", "matches": []}
        bid = state.get("business_id")
        if not bid:
            return {"error": "No business_id in state", "matches": []}
        try:
            from uuid import UUID

            from src.infrastructure.database.repositories.run_memory_digest_repository import (
                RunMemoryDigestRepository,
            )

            repo = RunMemoryDigestRepository(db_session)
            rows = await repo.search_similar(UUID(str(bid)), search_query, limit=5)
            return {"matches": rows}
        except Exception as e:
            logger.warning(f"search_similar_run_memories failed: {e}")
            return {"error": str(e), "matches": []}

    # ─── Build and return tools ────────────────────────────────────────────

    return [
        Tool(
            name="get_risk_thresholds",
            description="Get business-specific risk weights, thresholds, and guardrails configuration.",
            parameters=[],
            execute=get_risk_thresholds,
        ),
        Tool(
            name="compute_risk_features",
            description="Compute statistical risk features for ALL candidates: z-score, duplicates, velocity flags, budget ratio, round number detection.",
            parameters=[],
            execute=compute_risk_features,
        ),
        Tool(
            name="compute_velocity_features",
            description="Query 7-day and 30-day payout velocity for a specific candidate's account from historical data.",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to analyze",
                ),
            ],
            execute=compute_velocity_features,
        ),
        Tool(
            name="detect_round_number_bias",
            description="Check if a candidate's amount is suspiciously round (divisible by 10k/50k/100k with no kobo).",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to check",
                ),
            ],
            execute=detect_round_number_bias,
        ),
        Tool(
            name="cross_reference_beneficiaries",
            description="Find same-account-different-name patterns in historical data for a candidate.",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to cross-reference",
                ),
            ],
            execute=cross_reference_beneficiaries,
        ),
        Tool(
            name="compute_weighted_risk_score",
            description="Apply the weighted scoring model to a candidate's computed features. Returns computed score and suggested decision.",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to score",
                ),
            ],
            execute=compute_weighted_risk_score,
        ),
        Tool(
            name="score_candidate",
            description="Finalize risk score and decision for a candidate. Apply guardrails. Call this for EACH candidate after analysis.",
            parameters=[
                ToolParam(
                    name="candidate_id",
                    param_type=ToolParamType.STRING,
                    description="The candidate_id to score",
                ),
                ToolParam(
                    name="risk_score",
                    param_type=ToolParamType.NUMBER,
                    description="Final risk score from compute_weighted_risk_score (0.0-1.0)",
                ),
                ToolParam(
                    name="risk_decision",
                    param_type=ToolParamType.STRING,
                    description="Decision: allow, review, or block",
                ),
                ToolParam(
                    name="risk_reasons",
                    param_type=ToolParamType.STRING,
                    description="Pipe-separated risk reasons, e.g. 'high amount deviation|new beneficiary'",
                ),
                ToolParam(
                    name="adjustment",
                    param_type=ToolParamType.NUMBER,
                    description="Optional adjustment to score (±0.1 max). Default 0.",
                    required=False,
                ),
                ToolParam(
                    name="adjustment_reason",
                    param_type=ToolParamType.STRING,
                    description="Required justification if adjustment != 0",
                    required=False,
                ),
            ],
            execute=score_candidate,
        ),
        Tool(
            name="lookup_beneficiary_history",
            description="Look up detailed transaction and payout history for a candidate's beneficiary account.",
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
            name="get_beneficiary_reputation",
            description=(
                "Query historical payout reputation for a beneficiary by account number and bank code. "
                "Returns success rate, failure patterns, reputation score (0-1), and payout history. "
                "Use this to factor historical performance into risk scoring. "
                "Beneficiaries with success_rate < 0.5 or low reputation_score should receive elevated risk."
            ),
            parameters=[
                ToolParam(
                    name="account_number",
                    param_type=ToolParamType.STRING,
                    description="Beneficiary account number",
                    required=True,
                ),
                ToolParam(
                    name="bank_code",
                    param_type=ToolParamType.STRING,
                    description="Bank code (institution code)",
                    required=True,
                ),
            ],
            execute=get_beneficiary_reputation,
        ),
        Tool(
            name="search_similar_run_memories",
            description=(
                "Long-term memory: find past runs whose objective/summary matches a phrase "
                "(e.g. payroll, December, vendor). Use to spot recurring patterns and failures."
            ),
            parameters=[
                ToolParam(
                    name="search_query",
                    param_type=ToolParamType.STRING,
                    description="Phrase to match against past run objectives and digests",
                    required=True,
                ),
            ],
            execute=search_similar_run_memories,
        ),
    ]


# ============================================================================
# RiskAgent Class
# ============================================================================


class RiskAgent(BaseAgent):
    """
    Risk scoring agent with feature engineering and weighted scoring model.

    Phase 4 improvements:
    - Feature engineering: velocity, duplicates, amount deviation, round numbers, name consistency
    - Weighted scoring model with configurable weights per business
    - LLM reviews computed scores (not LLM-guessed scores)
    - Hard guardrails that cannot be overridden
    - Feature persistence for explainability
    """

    def __init__(self) -> None:
        super().__init__("RiskAgent")

    async def run(self, state: AgentState, db_session=None) -> AgentState:
        candidates = state.get("scored_candidates", [])
        risk_tolerance = state.get("risk_tolerance", 0.35)

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

        # Register tools
        self.registry = ToolRegistry()
        for tool in _build_risk_tools(state, db_session):
            self.registry.register(tool)

        # Build candidate summary for LLM
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

## Your Workflow:
1. Call `get_risk_thresholds` to understand the risk configuration
2. Call `compute_risk_features` to get features for all candidates
3. For candidates with risk signals, call `compute_velocity_features`, `cross_reference_beneficiaries`, or `detect_round_number_bias` as needed
4. Call `compute_weighted_risk_score` for each candidate to get the computed score
5. Call `score_candidate` for EACH candidate to finalize the assessment
6. Produce your final JSON with all scored candidates and a risk summary

Remember: You are REVIEWING computed scores, not guessing them. Adjustments must be justified."""

        try:
            await self.emit_progress(
                f"Scoring {len(candidates)} candidates with weighted model...",
                {
                    "candidate_count": len(candidates),
                    "risk_tolerance": risk_tolerance,
                },
            )

            # Run ReAct loop
            response = await self.reason_and_act_json(
                system_prompt=RISK_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

            # Parse response
            try:
                result = json.loads(response)
            except json.JSONDecodeError:
                result = {}

            # Ensure all candidates have scores (fallback)
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

            # Count decisions
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

            # Persist risk features if we have a DB session
            if db_session:
                await self._persist_risk_features(db_session, state, candidates)

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
                            "model_version": "v2.0-weighted",
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

    async def _persist_risk_features(
        self,
        db_session,
        state: AgentState,
        candidates: list[dict],
    ) -> None:
        """Persist computed risk features to RiskScoreFeatureModel for explainability."""
        try:
            from src.infrastructure.database.repositories.risk_feature_repository import (
                RiskFeatureRepository,
            )

            run_id = state.get("run_id")
            if not run_id:
                logger.warning("[RiskAgent] No run_id — skipping feature persistence")
                return

            rid = UUID(run_id) if isinstance(run_id, str) else run_id
            repo = RiskFeatureRepository(db_session)

            features_list = []
            for c in candidates:
                cid = c.get("candidate_id")
                if not cid:
                    continue

                # Get risk_features from candidate (populated by score_candidate tool)
                risk_features = c.get("risk_features", {})

                features_list.append(
                    {
                        "candidate_id": cid,
                        "historical_frequency": c.get("velocity_30d", 0),
                        "amount_deviation_ratio": risk_features.get("amount_z_score"),
                        "avg_historical_amount": c.get("avg_historical_amount"),
                        "duplicate_similarity_score": risk_features.get(
                            "duplicate_similarity"
                        ),
                        "lookup_mismatch_flag": risk_features.get(
                            "name_inconsistency", 0
                        )
                        > 0.5,
                        "account_anomaly_count": 0,  # Not computed yet
                        "account_age_days": c.get("account_age_days"),
                        "days_since_last_payout": c.get("days_since_last_payout"),
                        "amount_vs_budget_cap_pct": risk_features.get("amount_vs_cap"),
                    }
                )

            if features_list:
                await repo.batch_create(run_id=rid, features_list=features_list)
                logger.info(
                    f"[RiskAgent] Persisted {len(features_list)} risk feature records"
                )

        except Exception as e:
            logger.warning(f"[RiskAgent] Feature persistence failed: {e}")
            # Non-fatal — don't fail the run for persistence issues

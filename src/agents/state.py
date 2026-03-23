from typing import TypedDict, Optional, Annotated
from operator import add


class AgentState(TypedDict, total=False):
    run_id: str
    objective: str
    constraints: Optional[str]
    risk_tolerance: float
    budget_cap: Optional[float]
    merchant_id: str

    # PlannerAgent output
    plan_steps: list[dict]

    # ReconciliationAgent output
    transactions: list[dict]
    reconciled_ledger: dict
    unresolved_references: list[str]

    # RiskAgent output
    scored_candidates: list[dict]

    # ForecastAgent output
    forecast: Optional[dict]

    # ExecutionAgent output
    lookup_results: list[dict]
    payout_results: list[dict]
    payout_status_results: list[dict]

    # ApprovalGate
    approved_candidate_ids: list[str]
    rejected_candidate_ids: list[str]

    # AuditAgent output
    audit_report: Optional[dict]

    # Control flow
    current_step: str
    error: Optional[str]

    # Accumulator for audit log entries
    audit_entries: Annotated[list[dict], add]

from typing import TypedDict, Optional


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
    candidate_lookup_results: list[dict]
    candidate_execution_results: list[dict]
    batch_details: dict

    # ApprovalGate
    approved_candidate_ids: list[str]
    rejected_candidate_ids: list[str]

    # AuditAgent output
    audit_report: Optional[dict]

    # Control flow
    current_step: str
    error: Optional[str]

    # Accumulated audit log entries (managed by orchestrator, not LangGraph reducer)
    audit_entries: list[dict]

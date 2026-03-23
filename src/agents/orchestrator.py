"""
RunOrchestrator — Custom state machine for FlowPilot agent execution.

Replaces LangGraph's StateGraph with explicit step-by-step execution,
per-step DB persistence, approval gate halting, and resume-from-step.

Design principles:
  - Agents remain pure (return state, no DB awareness)
  - Orchestrator owns all persistence (plan_step, transaction, audit_log)
  - Approval gate is a state transition, not an agent node
  - Resume after approval executes only execute→audit (no re-run)
  - Each audit_log entry is correlated with its plan_step via step_id FK
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.state import AgentState
from src.agents.planner_agent import PlannerAgent
from src.agents.reconciliation_agent import ReconciliationAgent
from src.agents.risk_agent import RiskAgent
from src.agents.execution_agent import ExecutionAgent
from src.agents.audit_agent import AuditAgent
from src.infrastructure.database.repositories import (
    AuditRepository,
    CandidateRepository,
    PlanStepRepository,
    RunRepository,
    TransactionRepository,
)

logger = logging.getLogger(__name__)

# Singleton agent instances (stateless, safe to share)
_planner = PlannerAgent()
_reconciliation = ReconciliationAgent()
_risk = RiskAgent()
_execution = ExecutionAgent()
_audit = AuditAgent()

# Pipeline definition: (step_name, agent_type, run_status, agent_instance)
# run_status is the DB status to set DURING execution of this step.
# None means "don't update status" (caller controls final status).
_PIPELINE = [
    ("plan", "planner", "planning", _planner),
    ("reconcile", "reconciliation", "reconciling", _reconciliation),
    ("risk", "risk", "scoring", _risk),
    # approval_gate is handled as a state transition between risk and execute
    ("execute", "execution", "executing", _execution),
    ("audit", "audit", None, _audit),  # None: caller sets final status
]

# Steps that run before the approval gate
_PRE_APPROVAL_STEPS = {"plan", "reconcile", "risk"}

# Steps that run after the approval gate
_POST_APPROVAL_STEPS = {"execute", "audit"}


class RunOrchestrator:
    """Executes FlowPilot agent pipeline with per-step DB persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._run_repo = RunRepository(session)
        self._plan_step_repo = PlanStepRepository(session)
        self._transaction_repo = TransactionRepository(session)
        self._candidate_repo = CandidateRepository(session)
        self._audit_repo = AuditRepository(session)
        self._step_ids: dict[str, uuid.UUID] = {}  # agent_type → plan_step.id

    async def execute_run(
        self, run_id: uuid.UUID, state: AgentState
    ) -> AgentState:
        """Execute the full pre-approval pipeline: plan → reconcile → risk → [halt].

        Returns the state after halting at the approval gate (or after audit
        if an error occurs or no candidates need approval).
        """
        await self._run_repo.mark_started(run_id)

        for step_name, agent_type, run_status, agent in _PIPELINE:
            if step_name not in _PRE_APPROVAL_STEPS:
                continue

            state = await self._execute_step(
                run_id, step_name, agent_type, run_status, agent, state
            )

            if state.get("error"):
                state = await self._route_to_audit(run_id, state)
                return state

        # All pre-approval steps succeeded — enter approval gate
        await self._enter_approval_gate(run_id, state)
        return state

    async def resume_after_approval(
        self, run_id: uuid.UUID, state: AgentState
    ) -> AgentState:
        """Resume execution after operator approval: execute → audit ONLY.

        This is the critical difference from LangGraph — we skip plan/reconcile/risk
        entirely, avoiding double Interswitch API calls and double LLM costs.
        """
        # Load existing plan_step IDs so we can correlate audit entries
        await self._load_step_ids(run_id)

        for step_name, agent_type, run_status, agent in _PIPELINE:
            if step_name not in _POST_APPROVAL_STEPS:
                continue

            # Defer commit for the last step so final status + step data are atomic
            is_last = step_name == "audit"
            state = await self._execute_step(
                run_id, step_name, agent_type, run_status, agent, state,
                defer_commit=is_last,
            )

            if state.get("error") and step_name != "audit":
                state = await self._route_to_audit(run_id, state)
                return state

        # Pipeline complete — atomic commit with final step data
        final_status = "failed" if state.get("error") else "completed"
        await self._run_repo.update_status(run_id, final_status, state.get("error"))
        if final_status == "completed":
            await self._run_repo.mark_completed(run_id)
        await self._session.commit()

        return state

    # ------------------------------------------------------------------
    # Internal: step execution
    # ------------------------------------------------------------------

    async def _execute_step(
        self,
        run_id: uuid.UUID,
        step_name: str,
        agent_type: str,
        run_status: Optional[str],
        agent,
        state: AgentState,
        defer_commit: bool = False,
    ) -> AgentState:
        """Run one agent, persist results, and update step/run status."""
        logger.info(f"Run {run_id}: starting step '{step_name}'")

        # 1. Update run status (skip if None — caller controls)
        if run_status is not None:
            await self._run_repo.update_status(run_id, run_status)

        # 2. Mark plan_step as running (if it exists)
        step_id = self._step_ids.get(agent_type)
        if step_id is not None:
            await self._plan_step_repo.mark_started(step_id)

        # 3. Track pre-existing error to distinguish "this step failed" from "prior step lingering"
        error_before = state.get("error")

        # 4. Execute the agent
        try:
            result_state = await agent.run(state)
        except Exception as e:
            logger.error(f"Run {run_id}: agent '{step_name}' raised: {e}")
            # Preserve original error if one existed
            if error_before:
                state["error"] = f"{error_before} | {agent.name} also failed: {e}"
            else:
                state["error"] = f"{agent.name} failed: {e}"
            state["current_step"] = f"{step_name}_failed"
            if step_id is not None:
                await self._plan_step_repo.update_status(
                    step_id, "failed", error_message=str(e)
                )
            await self._session.commit()
            return state

        # 5. Guard: agent must return a dict
        if not isinstance(result_state, dict):
            err_msg = f"{agent.name} returned invalid state (got {type(result_state).__name__})"
            logger.error(f"Run {run_id}: {err_msg}")
            state["error"] = f"{error_before} | {err_msg}" if error_before else err_msg
            state["current_step"] = f"{step_name}_failed"
            if step_id is not None:
                await self._plan_step_repo.update_status(step_id, "failed", error_message=err_msg)
            await self._session.commit()
            return state

        # 6. Handle audit agent internal error return (doesn't re-raise)
        returned_error = result_state.get("error")
        if returned_error and returned_error != error_before:
            if error_before:
                result_state["error"] = f"{error_before} | {returned_error}"

        # 7. Extract audit entries BEFORE merging state (avoid overwrite)
        new_audit_entries = result_state.pop("audit_entries", [])

        # 8. Merge agent output into accumulated state
        state.update(result_state)
        state.setdefault("audit_entries", [])
        state["audit_entries"].extend(new_audit_entries)

        # 9. Persist step-specific artifacts (may populate self._step_ids for plan step)
        await self._persist_step_artifacts(run_id, step_name, state)

        # 10. Re-read step_id after artifact persistence (plan step creates step_ids)
        step_id = self._step_ids.get(agent_type)

        # 11. Determine if THIS step introduced a new error
        error_after = state.get("error")
        step_failed = error_after is not None and error_after != error_before

        # 12. Mark plan_step complete/failed (only blame THIS step for failures it caused)
        if step_id is not None:
            node_output = result_state if isinstance(result_state, dict) else None
            if step_failed:
                await self._plan_step_repo.update_status(
                    step_id, "failed",
                    output_data=node_output,
                    error_message=error_after,
                )
            else:
                await self._plan_step_repo.mark_completed(step_id, output_data=node_output)

        # 13. Persist audit entries with step_id correlation
        if new_audit_entries:
            await self._persist_audit_entries(run_id, step_id, new_audit_entries)

        # 14. Commit this step's changes (unless deferred for atomic finalization)
        if not defer_commit:
            await self._session.commit()
        logger.info(f"Run {run_id}: completed step '{step_name}'")

        return state

    # ------------------------------------------------------------------
    # Internal: artifact persistence per step type
    # ------------------------------------------------------------------

    async def _persist_step_artifacts(
        self, run_id: uuid.UUID, step_name: str, state: AgentState
    ) -> None:
        """Persist step-specific data to the appropriate tables."""
        if step_name == "plan" and state.get("plan_steps"):
            mapped_steps = _map_plan_steps(state["plan_steps"])
            persisted = await self._plan_step_repo.create_batch(run_id, mapped_steps)
            self._step_ids = {s.agent_type: s.id for s in persisted}

            # Store plan graph on agent_run
            await self._run_repo.update_plan_graph(
                run_id, {"steps": state["plan_steps"]}
            )

        elif step_name == "reconcile" and state.get("transactions"):
            mapped_txns = _map_transactions(state["transactions"])
            if mapped_txns:
                await self._transaction_repo.create_batch(run_id, mapped_txns)

        elif step_name == "risk" and state.get("scored_candidates"):
            mapped_candidates = _map_candidates(state["scored_candidates"])
            if mapped_candidates:
                await self._candidate_repo.create_batch(run_id, mapped_candidates)

    # ------------------------------------------------------------------
    # Internal: approval gate
    # ------------------------------------------------------------------

    async def _enter_approval_gate(
        self, run_id: uuid.UUID, state: AgentState
    ) -> None:
        """Transition run to awaiting_approval status."""
        state["current_step"] = "awaiting_approval"
        await self._run_repo.update_status(run_id, "awaiting_approval")
        await self._session.commit()
        logger.info(f"Run {run_id}: halted at approval gate")

    # ------------------------------------------------------------------
    # Internal: error routing
    # ------------------------------------------------------------------

    async def _route_to_audit(
        self, run_id: uuid.UUID, state: AgentState
    ) -> AgentState:
        """On error, skip remaining steps and run audit agent for the report."""
        logger.warning(f"Run {run_id}: error detected, routing to audit")

        # Load step IDs if not yet loaded (needed for audit step_id)
        if not self._step_ids:
            await self._load_step_ids(run_id)

        for step_name, agent_type, run_status, agent in _PIPELINE:
            if step_name != "audit":
                continue
            state = await self._execute_step(
                run_id, step_name, agent_type, run_status, agent, state,
                defer_commit=True,  # Atomic with final status update below
            )

        # Atomic: audit step data + final status in one commit
        final_status = "failed"
        await self._run_repo.update_status(run_id, final_status, state.get("error"))
        await self._session.commit()
        return state

    # ------------------------------------------------------------------
    # Internal: audit entry persistence with step_id correlation
    # ------------------------------------------------------------------

    async def _persist_audit_entries(
        self,
        run_id: uuid.UUID,
        step_id: Optional[uuid.UUID],
        entries: list[dict],
    ) -> None:
        """Persist audit entries to audit_log, injecting step_id FK."""
        rows = [
            {
                "run_id": run_id,
                "step_id": step_id,
                "agent_type": entry.get("agent_type"),
                "action": entry.get("action", "unknown"),
                "detail": entry.get("detail"),
                "api_endpoint": entry.get("api_endpoint"),
                "request_hash": entry.get("request_hash"),
                "response_status": entry.get("response_status"),
                "response_time_ms": entry.get("response_time_ms"),
            }
            for entry in entries
        ]
        await self._audit_repo.append_batch(rows)

    # ------------------------------------------------------------------
    # Internal: load existing step IDs from DB (for resume)
    # ------------------------------------------------------------------

    async def _load_step_ids(self, run_id: uuid.UUID) -> None:
        """Load plan_step IDs from DB into the step_ids map."""
        steps = await self._plan_step_repo.get_by_run(run_id)
        self._step_ids = {s.agent_type: s.id for s in steps}


# ======================================================================
# Pure mapping functions (no DB, no state mutation)
# ======================================================================


def _map_plan_steps(steps: list[dict]) -> list[dict]:
    """Map PlannerAgent output to PlanStepModel column names."""
    mapped = [
        {
            "agent_type": s.get("agent_type", ""),
            "step_order": s.get("order", idx + 1),
            "description": s.get("description"),
            "status": "pending",
        }
        for idx, s in enumerate(steps)
    ]
    # Ensure a planner step exists at order 0
    if not any(step["agent_type"] == "planner" for step in mapped):
        mapped.insert(0, {
            "agent_type": "planner",
            "step_order": 0,
            "description": "Generate execution plan",
            "status": "pending",
        })
    return mapped


def _map_transactions(txns: list[dict]) -> list[dict]:
    """Map Interswitch camelCase transaction dicts to DB snake_case columns."""
    mapped: list[dict] = []
    for t in txns:
        ref = t.get("transactionReference")
        amount = t.get("amount")
        status = t.get("status")
        if not ref or amount is None or status is None:
            continue
        mapped.append({
            "transaction_reference": ref,
            "amount": amount,
            "currency": t.get("currency", "NGN"),
            "status": status,
            "channel": t.get("channel"),
            "transaction_timestamp": t.get("timestamp"),
            "customer_id": t.get("customerId"),
            "merchant_id": t.get("merchantId"),
            "processor_response_code": t.get("processorResponseCode"),
            "processor_response_message": t.get("processorResponseMessage"),
            "settlement_date": t.get("settlementDate"),
            "is_anomaly": t.get("isAnomaly", False),
            "anomaly_reason": t.get("anomalyReason"),
        })
    return mapped


def _map_candidates(candidates: list[dict]) -> list[dict]:
    """Map RiskAgent scored_candidate dicts to PayoutCandidateModel column names."""
    mapped: list[dict] = []
    for c in candidates:
        mapped.append({
            "institution_code": c.get("institution_code"),
            "beneficiary_name": c.get("beneficiary_name"),
            "account_number": c.get("account_number"),
            "amount": c.get("amount"),
            "currency": c.get("currency", "NGN"),
            "purpose": c.get("purpose"),
            "risk_score": c.get("risk_score"),
            "risk_reasons": c.get("risk_reasons"),
            "risk_decision": c.get("risk_decision"),
            "approval_status": "pending",
        })
    return mapped

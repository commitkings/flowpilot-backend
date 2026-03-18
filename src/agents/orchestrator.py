"""
RunOrchestrator — Custom state machine for FlowPilot agent execution.

Replaces LangGraph's StateGraph with explicit step-by-step execution,
per-step DB persistence, approval gate halting, and resume-from-step.

Design principles:
  - Agents use a ReAct (Reason → Act → Observe) loop with tool calling
  - Orchestrator passes db_session to agents that need DB access for tools
  - Orchestrator owns all persistence (plan_step, transaction, audit_log)
  - Approval gate is a state transition, not an agent node
  - Resume after approval executes only execute→audit (no re-run)
  - Each audit_log entry is correlated with its plan_step via step_id FK
  - Tool call logs are accumulated across steps in state["tool_call_log"]
"""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.state import AgentState
from src.agents.planner_agent import PlannerAgent
from src.agents.reconciliation_agent import ReconciliationAgent
from src.agents.risk_agent import RiskAgent
from src.agents.execution_agent import ExecutionAgent
from src.agents.audit_agent import AuditAgent
from src.agents.event_publisher import EventPublisher, EventType
from src.infrastructure.database.repositories import (
    AuditRepository,
    BatchRepository,
    CandidateRepository,
    ExecutionDetailRepository,
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

AgentRegistryEntry = tuple[str, str | None, Any]
PipelineEntry = tuple[str, str, str | None, Any]

_AGENT_REGISTRY: dict[str, AgentRegistryEntry] = {
    "planner": ("plan", "planning", _planner),
    "reconciliation": ("reconcile", "reconciling", _reconciliation),
    "risk": ("risk", "scoring", _risk),
    "execution": ("execute", "executing", _execution),
    "audit": ("audit", None, _audit),
}


class RunOrchestrator:
    """Executes FlowPilot agent pipeline with per-step DB persistence."""

    def __init__(
        self, session: AsyncSession, publisher: Optional[EventPublisher] = None
    ) -> None:
        self._session = session
        self._publisher = publisher
        self._run_repo = RunRepository(session)
        self._plan_step_repo = PlanStepRepository(session)
        self._transaction_repo = TransactionRepository(session)
        self._candidate_repo = CandidateRepository(session)
        self._batch_repo = BatchRepository(session)
        self._audit_repo = AuditRepository(session)
        self._exec_detail_repo = ExecutionDetailRepository(session)
        self._step_ids: dict[str, uuid.UUID] = {}  # agent_type → plan_step.id

    async def execute_run(self, run_id: uuid.UUID, state: AgentState) -> AgentState:
        """Execute the full pre-approval pipeline: plan → reconcile → risk → [halt].

        Returns the state after halting at the approval gate (or after audit
        if an error occurs or no candidates need approval).
        """
        await self._run_repo.mark_started(run_id)

        if self._publisher:
            await self._publisher.run_started(state.get("objective", ""))

        planner_step_name, planner_status, planner_agent = _AGENT_REGISTRY["planner"]
        state = await self._execute_step(
            run_id,
            planner_step_name,
            "planner",
            planner_status,
            planner_agent,
            state,
        )

        if state.get("error"):
            state = await self._route_to_audit(run_id, state)
            return state

        dynamic_pipeline = self._build_dynamic_pipeline(state)
        has_execution = any(
            agent_type == "execution" for _, agent_type, _, _ in dynamic_pipeline
        )

        if has_execution:
            pre_approval_steps = [
                entry
                for entry in dynamic_pipeline
                if entry[1] not in {"execution", "audit"}
            ]
        else:
            pre_approval_steps = [
                entry for entry in dynamic_pipeline if entry[1] != "audit"
            ]

        for step_name, agent_type, run_status, agent in pre_approval_steps:
            self._apply_config_overrides(state, agent_type)

            state = await self._execute_step(
                run_id, step_name, agent_type, run_status, agent, state
            )

            if state.get("error"):
                state = await self._route_to_audit(run_id, state)
                return state

        if has_execution:
            await self._enter_approval_gate(run_id, state)
            return state

        audit_step_name, audit_status, audit_agent = _AGENT_REGISTRY["audit"]
        state = await self._execute_step(
            run_id,
            audit_step_name,
            "audit",
            audit_status,
            audit_agent,
            state,
            defer_commit=True,
        )

        final_status = "failed" if state.get("error") else "completed"
        await self._run_repo.update_status(run_id, final_status, state.get("error"))
        if final_status == "completed":
            await self._run_repo.mark_completed(run_id)

        if self._publisher:
            if final_status == "completed":
                await self._publisher.run_completed("Pipeline finished successfully")
            else:
                await self._publisher.run_failed(state.get("error", "Unknown error"))

        await self._session.commit()
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

        dynamic_pipeline = self._build_dynamic_pipeline(state)
        post_approval_steps = [
            entry for entry in dynamic_pipeline if entry[1] in {"execution", "audit"}
        ]

        for step_name, agent_type, run_status, agent in post_approval_steps:
            self._apply_config_overrides(state, agent_type)
            # Defer commit for the last step so final status + step data are atomic
            is_last = step_name == "audit"
            state = await self._execute_step(
                run_id,
                step_name,
                agent_type,
                run_status,
                agent,
                state,
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

        if self._publisher:
            if final_status == "completed":
                await self._publisher.run_completed("Pipeline finished successfully")
            else:
                await self._publisher.run_failed(state.get("error", "Unknown error"))

        await self._session.commit()

        return state

    def _build_dynamic_pipeline(self, state: AgentState) -> list[PipelineEntry]:
        """Build an execution pipeline from the planner's plan_steps output.

        Guardrails (non-negotiable):
        - Risk scoring always runs before execution
        - At least one analysis step (reconciliation or risk) must run
        - Audit always runs last (auto-appended)
        """
        plan_steps = state.get("plan_steps") or []
        sorted_steps = sorted(plan_steps, key=lambda step: step.get("order", 999))

        pipeline: list[PipelineEntry] = []
        for step in sorted_steps:
            agent_type = step.get("agent_type")

            if agent_type == "planner":
                logger.warning(
                    "Planner step found in plan_steps; skipping duplicate planner execution"
                )
                continue

            if agent_type == "audit":
                continue

            if agent_type not in _AGENT_REGISTRY:
                logger.warning(f"Unknown agent_type in plan: {agent_type}, skipping")
                continue

            step_name, run_status, agent_instance = _AGENT_REGISTRY[agent_type]
            pipeline.append((step_name, agent_type, run_status, agent_instance))

        if not pipeline:
            return self._default_dynamic_pipeline()

        pipeline = self._apply_guardrails(pipeline)

        audit_step_name, audit_status, audit_agent = _AGENT_REGISTRY["audit"]
        pipeline.append((audit_step_name, "audit", audit_status, audit_agent))
        return pipeline

    def _apply_guardrails(self, pipeline: list[PipelineEntry]) -> list[PipelineEntry]:
        """Enforce non-negotiable ordering and presence rules."""
        agent_types = [entry[1] for entry in pipeline]

        has_risk = "risk" in agent_types
        has_execution = "execution" in agent_types
        has_reconciliation = "reconciliation" in agent_types

        if not has_risk and not has_reconciliation:
            logger.warning(
                "Guardrail violation: no analysis step (reconciliation/risk). Injecting risk."
            )
            step_name, run_status, agent_instance = _AGENT_REGISTRY["risk"]
            pipeline.insert(0, (step_name, "risk", run_status, agent_instance))
            agent_types = [entry[1] for entry in pipeline]
            has_risk = True

        if has_execution and not has_risk:
            logger.warning(
                "Guardrail violation: execution without risk. Injecting risk before execution."
            )
            step_name, run_status, agent_instance = _AGENT_REGISTRY["risk"]
            exec_idx = agent_types.index("execution")
            pipeline.insert(exec_idx, (step_name, "risk", run_status, agent_instance))
            agent_types = [entry[1] for entry in pipeline]

        if has_execution and "risk" in agent_types:
            risk_idx = agent_types.index("risk")
            exec_idx = agent_types.index("execution")
            if risk_idx > exec_idx:
                logger.warning(
                    "Guardrail violation: risk after execution. Reordering risk before execution."
                )
                risk_entry = pipeline.pop(risk_idx)
                pipeline.insert(exec_idx, risk_entry)

        return pipeline

    def _default_dynamic_pipeline(self) -> list[PipelineEntry]:
        """Fallback pipeline when the planner fails to produce usable steps."""
        pipeline: list[PipelineEntry] = []
        for agent_type in ("reconciliation", "risk", "execution", "audit"):
            step_name, run_status, agent_instance = _AGENT_REGISTRY[agent_type]
            pipeline.append((step_name, agent_type, run_status, agent_instance))
        return pipeline

    def _apply_config_overrides(self, state: AgentState, agent_type: str) -> None:
        """Merge plan-specified config overrides into the shared state."""
        for step in state.get("plan_steps", []):
            if step.get("agent_type") != agent_type:
                continue

            overrides = step.get("config_overrides")
            if not overrides:
                return

            if not isinstance(overrides, dict):
                logger.warning(
                    f"Invalid config_overrides for agent_type={agent_type}: expected dict"
                )
                return

            state.update(overrides)
            return

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

        # --- Emit step_started event ---
        if self._publisher:
            desc = f"Running {agent.name}"
            await self._publisher.step_started(
                step_name, agent_type, desc, step_id=step_id
            )
            # Pass publisher + step_id to agent for reasoning/progress events
            if hasattr(agent, "set_publisher"):
                agent.set_publisher(self._publisher, step_id=step_id)

        # 4. Execute the agent (with timing)
        #    Pass db_session so agents with DB-backed tools can query directly
        t0 = time.monotonic()
        try:
            result_state = await agent.run(state, db_session=self._session)
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.error(f"Run {run_id}: agent '{step_name}' raised: {e}")
            # Preserve original error if one existed
            if error_before:
                state["error"] = f"{error_before} | {agent.name} also failed: {e}"
            else:
                state["error"] = f"{agent.name} failed: {e}"
            state["current_step"] = f"{step_name}_failed"
            if self._publisher:
                await self._publisher.step_failed(
                    step_name,
                    agent_type,
                    str(e),
                    duration_ms=elapsed_ms,
                    step_id=step_id,
                )
            if step_id is not None:
                await self._plan_step_repo.update_status(
                    step_id, "failed", error_message=str(e)
                )
            await self._session.commit()
            return state
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # 5. Guard: agent must return a dict
        if not isinstance(result_state, dict):
            err_msg = f"{agent.name} returned invalid state (got {type(result_state).__name__})"
            logger.error(f"Run {run_id}: {err_msg}")
            state["error"] = f"{error_before} | {err_msg}" if error_before else err_msg
            state["current_step"] = f"{step_name}_failed"
            if step_id is not None:
                await self._plan_step_repo.update_status(
                    step_id, "failed", error_message=err_msg
                )
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

        # 8b. Harvest reasoning entries from agent into state
        state.setdefault("reasoning_log", [])
        if hasattr(agent, "_reasoning_entries"):
            state["reasoning_log"].extend(agent._reasoning_entries)

        # 8c. Harvest tool call log entries from agent's registry
        state.setdefault("tool_call_log", [])
        if hasattr(agent, "registry") and hasattr(agent.registry, "call_log"):
            state["tool_call_log"].extend(agent.registry.call_log)

        # 9. Persist step-specific artifacts (may populate self._step_ids for plan step)
        #    Returns True if step status/output_data was already finalized.
        step_finalized = await self._persist_step_artifacts(run_id, step_name, state)

        # 10. Re-read step_id after artifact persistence (plan step creates step_ids)
        step_id = self._step_ids.get(agent_type)

        # 11. Determine if THIS step introduced a new error
        error_after = state.get("error")
        step_failed = error_after is not None and error_after != error_before

        # 12. Mark plan_step complete/failed (skip if _persist_step_artifacts already finalized)
        if step_id is not None and not step_finalized:
            node_output = result_state if isinstance(result_state, dict) else None
            if step_failed:
                await self._plan_step_repo.update_status(
                    step_id,
                    "failed",
                    output_data=node_output,
                    error_message=error_after,
                )
            else:
                await self._plan_step_repo.mark_completed(
                    step_id, output_data=node_output
                )

        # 13. Persist audit entries with step_id correlation
        if new_audit_entries:
            await self._persist_audit_entries(run_id, step_id, new_audit_entries)

        # --- Emit step_completed or step_failed event ---
        if self._publisher:
            if step_failed:
                await self._publisher.step_failed(
                    step_name,
                    agent_type,
                    error_after or "Unknown error",
                    duration_ms=elapsed_ms,
                    step_id=step_id,
                )
            else:
                summary = _step_summary(step_name, state)
                await self._publisher.step_completed(
                    step_name,
                    agent_type,
                    elapsed_ms,
                    summary,
                    step_id=step_id,
                )

        # 14. Commit this step's changes (unless deferred for atomic finalization)
        if not defer_commit:
            await self._session.commit()
        logger.info(f"Run {run_id}: completed step '{step_name}' in {elapsed_ms}ms")

        return state

    # ------------------------------------------------------------------
    # Internal: artifact persistence per step type
    # ------------------------------------------------------------------

    async def _persist_step_artifacts(
        self, run_id: uuid.UUID, step_name: str, state: AgentState
    ) -> bool:
        """Persist step-specific data to the appropriate tables.

        Returns True if the step's status/output_data was already finalized here
        (caller should skip redundant mark_completed).
        """
        if step_name == "plan" and state.get("plan_steps"):
            mapped_steps = _map_plan_steps(state["plan_steps"])
            persisted = await self._plan_step_repo.create_batch(run_id, mapped_steps)
            self._step_ids = {s.agent_type: s.id for s in persisted}

            # Store plan graph on agent_run
            await self._run_repo.update_plan_graph(
                run_id, {"steps": state["plan_steps"]}
            )
            return False

        elif step_name == "reconcile" and state.get("transactions"):
            mapped_txns = _map_transactions(state["transactions"])
            if mapped_txns:
                business_id = (
                    uuid.UUID(state["business_id"])
                    if state.get("business_id")
                    else None
                )
                await self._transaction_repo.create_batch(
                    run_id, business_id, mapped_txns
                )

            # Persist reconciliation summary on plan_step output_data
            resolved = state.get("resolved_references", [])
            recon_step_id = self._step_ids.get("reconciliation")
            if recon_step_id is not None:
                await self._plan_step_repo.mark_completed(
                    recon_step_id,
                    output_data={
                        "reconciled_ledger": state.get("reconciled_ledger", {}),
                        "unresolved_references": state.get("unresolved_references", []),
                        "resolved_count": len(resolved),
                        "total_transactions": len(state.get("transactions", [])),
                    },
                )
                return True
            return False

        elif step_name == "risk" and state.get("scored_candidates"):
            # Candidates already exist in DB (ingested at run creation).
            # Update each row with risk scores from the RiskAgent.
            skipped = 0
            for c in state["scored_candidates"]:
                candidate_id = c.get("candidate_id")
                if not candidate_id:
                    skipped += 1
                    continue
                try:
                    await self._candidate_repo.update_risk_scoring(
                        candidate_id=uuid.UUID(candidate_id),
                        risk_score=Decimal(str(c.get("risk_score", 0.5))),
                        risk_reasons=c.get("risk_reasons", []),
                        risk_decision=c.get("risk_decision", "review"),
                        run_id=run_id,
                    )
                except Exception as e:
                    logger.warning(
                        f"Run {run_id}: failed to update risk for candidate {candidate_id}: {e}"
                    )
            if skipped:
                logger.warning(
                    f"Run {run_id}: {skipped} candidates skipped (missing candidate_id)"
                )
            return False

        elif step_name == "execute":
            await self._persist_execution_artifacts(run_id, state)
            return False

        return False

    async def _persist_execution_artifacts(
        self, run_id: uuid.UUID, state: AgentState
    ) -> None:
        """Persist lookup results, payout_batch, and execution references."""
        from datetime import datetime, timezone

        persist_errors: list[str] = []

        # 1. Persist lookup results on payout_candidate rows + detail table
        for lr in state.get("candidate_lookup_results", []):
            candidate_id = lr.get("candidate_id")
            if not candidate_id:
                continue
            try:
                match_score = lr.get("lookup_match_score")
                txn_ref = lr.get("transaction_reference")
                await self._candidate_repo.update_lookup(
                    candidate_id=uuid.UUID(candidate_id),
                    lookup_status=lr.get("lookup_status", "failed"),
                    lookup_account_name=lr.get("lookup_account_name"),
                    lookup_match_score=(
                        Decimal(str(match_score)) if match_score is not None else None
                    ),
                    transaction_reference=txn_ref,
                )
                # Write detail record to customer_lookup_result
                await self._exec_detail_repo.create_lookup_result(
                    candidate_id=uuid.UUID(candidate_id),
                    run_id=run_id,
                    account_number=lr.get("lookup_account_name", ""),  # from raw data
                    institution_code=lr.get("institution_code", ""),
                    can_credit=lr.get("lookup_status") == "success"
                    or lr.get("lookup_status") == "mismatch",
                    name_returned=lr.get("lookup_account_name"),
                    similarity_score=(
                        Decimal(str(match_score)) if match_score is not None else None
                    ),
                    transaction_reference=txn_ref,
                    http_status_code=200,
                    response_message=lr.get("lookup_status"),
                    raw_response=lr.get("raw_response", {}),
                )
            except Exception as e:
                msg = f"lookup persist for {candidate_id}: {e}"
                logger.warning(f"Run {run_id}: {msg}")
                persist_errors.append(msg)

        # 2. Create payout_batch record
        batch_details = state.get("batch_details")
        batch_id = None
        if batch_details:
            item_count = batch_details.get("item_count", 0)
            if item_count < 1:
                msg = f"batch skipped: item_count={item_count}"
                logger.error(f"Run {run_id}: {msg}")
                persist_errors.append(msg)
            else:
                try:
                    business_id = (
                        uuid.UUID(state["business_id"])
                        if state.get("business_id")
                        else run_id
                    )
                    batch = await self._batch_repo.create(
                        run_id=run_id,
                        business_id=business_id,
                        batch_reference=batch_details["batch_reference"],
                        currency=batch_details.get("currency", "NGN"),
                        source_account_id=batch_details.get("source_account_id", ""),
                        total_amount=Decimal(str(batch_details.get("total_amount", 0))),
                        item_count=item_count,
                        submission_status=batch_details.get(
                            "submission_status", "pending"
                        ),
                        accepted_count=batch_details.get("accepted_count", 0),
                        rejected_count=batch_details.get("rejected_count", 0),
                    )
                    batch_id = batch.id
                except Exception as e:
                    msg = f"batch persist: {e}"
                    logger.error(f"Run {run_id}: {msg}")
                    persist_errors.append(msg)

        # 3. Persist execution results on payout_candidate rows + detail table
        now = datetime.now(timezone.utc)
        for er in state.get("candidate_execution_results", []):
            candidate_id = er.get("candidate_id")
            if not candidate_id:
                continue
            exec_status = er.get("execution_status", "pending")
            # Only link to batch and set executed_at for candidates that were actually submitted
            was_submitted = exec_status != "requires_followup"
            try:
                await self._candidate_repo.update_execution(
                    candidate_id=uuid.UUID(candidate_id),
                    execution_status=exec_status,
                    client_reference=er.get("client_reference"),
                    provider_reference=er.get("provider_reference"),
                    batch_id=batch_id if was_submitted else None,
                    executed_at=now if was_submitted else None,
                )
                # Write detail record to payout_execution
                if was_submitted:
                    await self._exec_detail_repo.create_payout_execution(
                        candidate_id=uuid.UUID(candidate_id),
                        run_id=run_id,
                        submission_type="submission",
                        interswitch_reference=er.get("provider_reference"),
                        execution_status=exec_status,
                        http_status_code=200,
                        response_message=er.get("response_message", ""),
                        raw_response={},
                        called_at=now,
                    )
            except Exception as e:
                msg = f"execution persist for {candidate_id}: {e}"
                logger.warning(f"Run {run_id}: {msg}")
                persist_errors.append(msg)

        # Propagate critical persistence failures to state so run doesn't silently complete
        if persist_errors:
            err_summary = f"Execution artifact persistence errors: {'; '.join(persist_errors[:5])}"
            logger.error(f"Run {run_id}: {err_summary}")
            existing_error = state.get("error")
            if existing_error:
                state["error"] = f"{existing_error} | {err_summary}"
            else:
                state["error"] = err_summary

    # ------------------------------------------------------------------
    # Internal: approval gate
    # ------------------------------------------------------------------

    async def _enter_approval_gate(self, run_id: uuid.UUID, state: AgentState) -> None:
        """Transition run to awaiting_approval status."""
        state["current_step"] = "awaiting_approval"
        await self._run_repo.update_status(run_id, "awaiting_approval")

        if self._publisher:
            n_candidates = len(state.get("candidates", []))
            await self._publisher.approval_gate(
                {
                    "candidates_count": n_candidates,
                    "message": f"{n_candidates} candidate(s) awaiting approval",
                }
            )

        await self._session.commit()
        logger.info(f"Run {run_id}: halted at approval gate")

    # ------------------------------------------------------------------
    # Internal: error routing
    # ------------------------------------------------------------------

    async def _route_to_audit(self, run_id: uuid.UUID, state: AgentState) -> AgentState:
        """On error, skip remaining steps and run audit agent for the report."""
        logger.warning(f"Run {run_id}: error detected, routing to audit")

        # Load step IDs if not yet loaded (needed for audit step_id)
        if not self._step_ids:
            await self._load_step_ids(run_id)

        step_name, run_status, agent = _AGENT_REGISTRY["audit"]
        state = await self._execute_step(
            run_id,
            step_name,
            "audit",
            run_status,
            agent,
            state,
            defer_commit=True,
        )

        # Atomic: audit step data + final status in one commit
        final_status = "failed"
        await self._run_repo.update_status(run_id, final_status, state.get("error"))

        if self._publisher:
            await self._publisher.run_failed(state.get("error", "Unknown error"))

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


def _step_summary(step_name: str, state: AgentState) -> str:
    """Generate a concise summary string for a completed step."""
    if step_name == "plan":
        n = len(state.get("plan_steps", []))
        return f"Generated {n} step(s)"
    elif step_name == "reconcile":
        n = len(state.get("transactions", []))
        ledger = state.get("reconciled_ledger", {})
        unresolved = len(state.get("unresolved_references", []))
        return f"Reconciled {n} transaction(s), {unresolved} unresolved"
    elif step_name == "risk":
        n = len(state.get("scored_candidates", []))
        return f"Scored {n} candidate(s)"
    elif step_name == "execute":
        n = len(state.get("candidate_execution_results", []))
        batch = state.get("batch_details") or {}
        ref = batch.get("batch_reference", "")
        return f"Executed {n} payout(s)" + (f", batch {ref[:20]}" if ref else "")
    elif step_name == "audit":
        report = state.get("audit_report") or {}
        return (report.get("executive_summary") or "Audit complete")[:120]
    return "Step completed"


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
        mapped.insert(
            0,
            {
                "agent_type": "planner",
                "step_order": 0,
                "description": "Generate execution plan",
                "status": "pending",
            },
        )
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
        mapped.append(
            {
                "interswitch_ref": ref,
                "amount": amount,
                "currency": t.get("currency", "NGN"),
                "direction": t.get("direction", "inflow"),
                "status": status,
                "channel": t.get("channel"),
                "narration": t.get("narration"),
                "transaction_timestamp": t.get("timestamp"),
                "settlement_date": t.get("settlementDate"),
                "counterparty_name": t.get("counterpartyName"),
                "counterparty_bank": t.get("counterpartyBank"),
                "has_anomaly": t.get("isAnomaly", False),
                "anomaly_count": len(t.get("anomalies", []))
                if t.get("isAnomaly")
                else 0,
            }
        )
    return mapped


def _map_candidates(candidates: list[dict]) -> list[dict]:
    """Map RiskAgent scored_candidate dicts to PayoutCandidateModel column names."""
    mapped: list[dict] = []
    for c in candidates:
        mapped.append(
            {
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
            }
        )
    return mapped

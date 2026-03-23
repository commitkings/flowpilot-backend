import logging

from langgraph.graph import StateGraph, END

from src.agents.state import AgentState
from src.agents.planner_agent import PlannerAgent
from src.agents.reconciliation_agent import ReconciliationAgent
from src.agents.risk_agent import RiskAgent
from src.agents.execution_agent import ExecutionAgent
from src.agents.audit_agent import AuditAgent

logger = logging.getLogger(__name__)

planner = PlannerAgent()
reconciliation = ReconciliationAgent()
risk = RiskAgent()
execution = ExecutionAgent()
audit = AuditAgent()


async def plan_node(state: AgentState) -> AgentState:
    return await planner.run(state)


async def reconcile_node(state: AgentState) -> AgentState:
    return await reconciliation.run(state)


async def risk_node(state: AgentState) -> AgentState:
    return await risk.run(state)


async def approval_gate_node(state: AgentState) -> AgentState:
    logger.info("[ApprovalGate] Waiting for operator approval")
    return {
        **state,
        "current_step": "awaiting_approval",
    }


async def execute_node(state: AgentState) -> AgentState:
    return await execution.run(state)


async def audit_node(state: AgentState) -> AgentState:
    return await audit.run(state)


def should_continue_after_plan(state: AgentState) -> str:
    if state.get("error"):
        return "audit"
    return "reconcile"


def should_continue_after_reconcile(state: AgentState) -> str:
    if state.get("error"):
        return "audit"
    return "risk"


def should_continue_after_risk(state: AgentState) -> str:
    if state.get("error"):
        return "audit"
    return "approval_gate"


def should_continue_after_approval(state: AgentState) -> str:
    if state.get("approved_candidate_ids") or any(
        c.get("risk_decision") == "allow" for c in state.get("scored_candidates", [])
    ):
        return "execute"
    return "audit"


def should_continue_after_execute(state: AgentState) -> str:
    return "audit"


def build_flowpilot_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("plan", plan_node)
    graph.add_node("reconcile", reconcile_node)
    graph.add_node("risk", risk_node)
    graph.add_node("approval_gate", approval_gate_node)
    graph.add_node("execute", execute_node)
    graph.add_node("audit", audit_node)

    graph.set_entry_point("plan")

    graph.add_conditional_edges("plan", should_continue_after_plan, {
        "reconcile": "reconcile",
        "audit": "audit",
    })
    graph.add_conditional_edges("reconcile", should_continue_after_reconcile, {
        "risk": "risk",
        "audit": "audit",
    })
    graph.add_conditional_edges("risk", should_continue_after_risk, {
        "approval_gate": "approval_gate",
        "audit": "audit",
    })
    graph.add_conditional_edges("approval_gate", should_continue_after_approval, {
        "execute": "execute",
        "audit": "audit",
    })
    graph.add_conditional_edges("execute", should_continue_after_execute, {
        "audit": "audit",
    })
    graph.add_edge("audit", END)

    return graph.compile()

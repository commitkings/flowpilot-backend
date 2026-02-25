from datetime import datetime
from typing import Optional
from enum import Enum

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RECONCILING = "reconciling"
    SCORING = "scoring"
    FORECASTING = "forecasting"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRun(BaseModel):
    run_id: str = Field(..., description="Unique identifier for this agent run")
    objective: str = Field(..., description="Operator-provided objective text")
    constraints: Optional[str] = Field(None, description="Optional constraints for the run")
    risk_tolerance: float = Field(0.35, ge=0.0, le=1.0, description="Maximum acceptable risk score")
    budget_cap: Optional[float] = Field(None, description="Maximum payout budget in NGN")
    status: RunStatus = Field(default=RunStatus.PENDING)
    plan_graph: Optional[dict] = Field(None, description="Executable plan from PlannerAgent")
    created_by: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        use_enum_values = True

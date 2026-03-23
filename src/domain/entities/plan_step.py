from datetime import datetime
from typing import Optional, Any
from enum import Enum

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentType(str, Enum):
    PLANNER = "planner"
    RECONCILIATION = "reconciliation"
    RISK = "risk"
    FORECAST = "forecast"
    EXECUTION = "execution"
    AUDIT = "audit"


class PlanStep(BaseModel):
    step_id: str = Field(..., description="Unique step identifier")
    run_id: str = Field(..., description="Parent agent run ID")
    agent_type: AgentType
    order: int = Field(..., ge=0, description="Execution order")
    status: StepStatus = Field(default=StepStatus.PENDING)
    input_data: Optional[dict[str, Any]] = None
    output_data: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    class Config:
        use_enum_values = True

from typing import Optional
from enum import Enum

from pydantic import BaseModel, Field


class LookupStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    MISMATCH = "mismatch"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVIEW = "review"


class ExecutionStatus(str, Enum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    REQUIRES_FOLLOWUP = "requires_followup"


class RiskDecision(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    BLOCK = "block"


class PayoutCandidate(BaseModel):
    candidate_id: str = Field(..., description="Unique candidate identifier")
    run_id: str = Field(..., description="Parent agent run ID")
    beneficiary_name: str = Field(..., description="Recipient name")
    institution_code: str = Field(..., description="Bank/institution code")
    account_number: str = Field(..., description="Recipient account number")
    amount: float = Field(..., gt=0, description="Payout amount in NGN")
    currency: str = Field(default="NGN")
    purpose: Optional[str] = Field(None, description="Payout narration/purpose")
    risk_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    risk_reasons: list[str] = Field(default_factory=list)
    risk_decision: Optional[RiskDecision] = None
    lookup_status: LookupStatus = Field(default=LookupStatus.PENDING)
    lookup_account_name: Optional[str] = None
    lookup_match_score: Optional[float] = None
    approval_status: ApprovalStatus = Field(default=ApprovalStatus.PENDING)
    execution_reference: Optional[str] = None
    provider_reference: Optional[str] = None
    execution_status: ExecutionStatus = Field(default=ExecutionStatus.NOT_STARTED)

    class Config:
        use_enum_values = True

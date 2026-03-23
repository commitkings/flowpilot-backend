import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.infrastructure.database.base import Base


# --------------------------------------------------------------------------- #
# 1. operator
# --------------------------------------------------------------------------- #
class OperatorModel(Base):
    __tablename__ = "operator"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    external_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    role: Mapped[str] = mapped_column(Text, server_default=text("'analyst'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('analyst', 'approver', 'admin')",
            name="operator_role_check",
        ),
    )

    agent_runs: Mapped[list["AgentRunModel"]] = relationship(
        back_populates="operator",
    )
    approved_candidates: Mapped[list["PayoutCandidateModel"]] = relationship(
        back_populates="approved_by_operator",
    )


# --------------------------------------------------------------------------- #
# 2. institution
# --------------------------------------------------------------------------- #
class InstitutionModel(Base):
    __tablename__ = "institution"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    institution_code: Mapped[str] = mapped_column(String(10), unique=True)
    institution_name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    payout_candidates: Mapped[list["PayoutCandidateModel"]] = relationship(
        back_populates="institution",
    )


# --------------------------------------------------------------------------- #
# 3. agent_run
# --------------------------------------------------------------------------- #
class AgentRunModel(Base):
    __tablename__ = "agent_run"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    operator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("operator.id", ondelete="RESTRICT"),
    )
    objective: Mapped[str] = mapped_column(Text)
    constraints: Mapped[Optional[str]] = mapped_column(Text)
    risk_tolerance: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), server_default=text("0.35")
    )
    budget_cap: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    merchant_id: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    plan_graph: Mapped[Optional[dict]] = mapped_column(JSONB)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "risk_tolerance >= 0.00 AND risk_tolerance <= 1.00",
            name="agent_run_risk_tolerance_check",
        ),
        CheckConstraint("budget_cap >= 0", name="agent_run_budget_cap_check"),
        CheckConstraint(
            "status IN ('pending', 'planning', 'reconciling', 'scoring', "
            "'forecasting', 'awaiting_approval', 'executing', "
            "'completed', 'failed', 'cancelled')",
            name="agent_run_status_check",
        ),
    )

    operator: Mapped["OperatorModel"] = relationship(back_populates="agent_runs")
    plan_steps: Mapped[list["PlanStepModel"]] = relationship(
        back_populates="agent_run",
    )
    transactions: Mapped[list["TransactionModel"]] = relationship(
        back_populates="agent_run",
    )
    payout_batches: Mapped[list["PayoutBatchModel"]] = relationship(
        back_populates="agent_run",
    )
    payout_candidates: Mapped[list["PayoutCandidateModel"]] = relationship(
        back_populates="agent_run",
    )
    audit_logs: Mapped[list["AuditLogModel"]] = relationship(
        back_populates="agent_run",
    )


# --------------------------------------------------------------------------- #
# 4. plan_step
# --------------------------------------------------------------------------- #
class PlanStepModel(Base):
    __tablename__ = "plan_step"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    agent_type: Mapped[str] = mapped_column(Text)
    step_order: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    input_data: Mapped[Optional[dict]] = mapped_column(JSONB)
    output_data: Mapped[Optional[dict]] = mapped_column(JSONB)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id", "step_order", name="plan_step_run_id_step_order_unique"
        ),
        CheckConstraint(
            "agent_type IN ('planner', 'reconciliation', 'risk', "
            "'forecast', 'execution', 'audit')",
            name="plan_step_agent_type_check",
        ),
        CheckConstraint("step_order >= 0", name="plan_step_step_order_check"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped')",
            name="plan_step_status_check",
        ),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(back_populates="plan_steps")
    audit_logs: Mapped[list["AuditLogModel"]] = relationship(
        back_populates="plan_step",
    )


# --------------------------------------------------------------------------- #
# 5. transaction
# --------------------------------------------------------------------------- #
class TransactionModel(Base):
    __tablename__ = "transaction"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    transaction_reference: Mapped[str] = mapped_column(String(100))
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    status: Mapped[str] = mapped_column(Text)
    channel: Mapped[Optional[str]] = mapped_column(Text)
    transaction_timestamp: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    customer_id: Mapped[Optional[str]] = mapped_column(String(100))
    merchant_id: Mapped[Optional[str]] = mapped_column(String(50))
    processor_response_code: Mapped[Optional[str]] = mapped_column(String(10))
    processor_response_message: Mapped[Optional[str]] = mapped_column(Text)
    settlement_date: Mapped[Optional[date]] = mapped_column(Date)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    anomaly_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "transaction_reference",
            name="transaction_run_id_reference_unique",
        ),
        CheckConstraint("amount >= 0", name="transaction_amount_check"),
        CheckConstraint(
            "status IN ('SUCCESS', 'PENDING', 'FAILED', 'REVERSED')",
            name="transaction_status_check",
        ),
        CheckConstraint(
            "channel IN ('CARD', 'TRANSFER', 'USSD', 'QR')",
            name="transaction_channel_check",
        ),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(back_populates="transactions")


# --------------------------------------------------------------------------- #
# 6. payout_batch
# --------------------------------------------------------------------------- #
class PayoutBatchModel(Base):
    __tablename__ = "payout_batch"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    batch_reference: Mapped[str] = mapped_column(String(100), unique=True)
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    source_account_id: Mapped[str] = mapped_column(String(100))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    item_count: Mapped[int] = mapped_column(SmallInteger)
    accepted_count: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0")
    )
    rejected_count: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0")
    )
    submission_status: Mapped[str] = mapped_column(
        Text, server_default=text("'pending'")
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("total_amount >= 0", name="payout_batch_total_amount_check"),
        CheckConstraint("item_count > 0", name="payout_batch_item_count_check"),
        CheckConstraint(
            "submission_status IN "
            "('pending', 'accepted', 'partial', 'rejected', 'failed')",
            name="payout_batch_submission_status_check",
        ),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(back_populates="payout_batches")
    payout_candidates: Mapped[list["PayoutCandidateModel"]] = relationship(
        back_populates="payout_batch",
    )


# --------------------------------------------------------------------------- #
# 7. payout_candidate
# --------------------------------------------------------------------------- #
class PayoutCandidateModel(Base):
    __tablename__ = "payout_candidate"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payout_batch.id", ondelete="SET NULL"),
    )
    institution_code: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("institution.institution_code", ondelete="RESTRICT"),
    )
    beneficiary_name: Mapped[str] = mapped_column(String(255))
    account_number: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    purpose: Mapped[Optional[str]] = mapped_column(String(255))

    risk_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    risk_reasons: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    risk_decision: Mapped[Optional[str]] = mapped_column(Text)

    lookup_status: Mapped[str] = mapped_column(
        Text, server_default=text("'pending'")
    )
    lookup_account_name: Mapped[Optional[str]] = mapped_column(String(255))
    lookup_match_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))

    approval_status: Mapped[str] = mapped_column(
        Text, server_default=text("'pending'")
    )
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("operator.id", ondelete="SET NULL"),
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    execution_status: Mapped[str] = mapped_column(
        Text, server_default=text("'not_started'")
    )
    client_reference: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    provider_reference: Mapped[Optional[str]] = mapped_column(String(100))
    executed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="payout_candidate_amount_check"),
        CheckConstraint(
            "risk_score >= 0.000 AND risk_score <= 1.000",
            name="payout_candidate_risk_score_check",
        ),
        CheckConstraint(
            "risk_decision IN ('allow', 'review', 'block')",
            name="payout_candidate_risk_decision_check",
        ),
        CheckConstraint(
            "lookup_status IN ('pending', 'success', 'failed', 'mismatch')",
            name="payout_candidate_lookup_status_check",
        ),
        CheckConstraint(
            "approval_status IN ('pending', 'approved', 'rejected')",
            name="payout_candidate_approval_status_check",
        ),
        CheckConstraint(
            "execution_status IN ('not_started', 'pending', 'success', "
            "'failed', 'requires_followup')",
            name="payout_candidate_execution_status_check",
        ),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(
        back_populates="payout_candidates",
    )
    payout_batch: Mapped[Optional["PayoutBatchModel"]] = relationship(
        back_populates="payout_candidates",
    )
    institution: Mapped["InstitutionModel"] = relationship(
        back_populates="payout_candidates",
    )
    approved_by_operator: Mapped[Optional["OperatorModel"]] = relationship(
        back_populates="approved_candidates",
    )


# --------------------------------------------------------------------------- #
# 8. audit_log (immutable — no updated_at)
# --------------------------------------------------------------------------- #
class AuditLogModel(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    step_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_step.id", ondelete="SET NULL"),
    )
    agent_type: Mapped[Optional[str]] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[Optional[dict]] = mapped_column(JSONB)
    api_endpoint: Mapped[Optional[str]] = mapped_column(String(255))
    request_hash: Mapped[Optional[str]] = mapped_column(CHAR(64))
    response_status: Mapped[Optional[int]] = mapped_column(SmallInteger)
    response_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "agent_type IN ('planner', 'reconciliation', 'risk', "
            "'forecast', 'execution', 'audit')",
            name="audit_log_agent_type_check",
        ),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(back_populates="audit_logs")
    plan_step: Mapped[Optional["PlanStepModel"]] = relationship(
        back_populates="audit_logs",
    )

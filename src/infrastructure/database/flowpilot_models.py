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
    Index,
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


# =========================================================================== #
#  AUTH & IDENTITY (2 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 1. user — local record, auth delegated to external provider
# --------------------------------------------------------------------------- #
class UserModel(Base):
    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    external_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    avatar_url: Mapped[Optional[str]] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    memberships: Mapped[list["BusinessMemberModel"]] = relationship(
        back_populates="user",
    )

    # Back-references from tables that FK to user
    created_runs: Mapped[list["AgentRunModel"]] = relationship(
        back_populates="creator",
        foreign_keys="AgentRunModel.created_by",
    )
    approved_runs: Mapped[list["AgentRunModel"]] = relationship(
        back_populates="approver",
        foreign_keys="AgentRunModel.approved_by",
    )
    approved_candidates: Mapped[list["PayoutCandidateModel"]] = relationship(
        back_populates="approved_by_user",
    )


# --------------------------------------------------------------------------- #
# 2. business_member — M:N user ↔ business with role
# --------------------------------------------------------------------------- #
class BusinessMemberModel(Base):
    __tablename__ = "business_member"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
    )
    role: Mapped[str] = mapped_column(Text, server_default=text("'analyst'"))
    joined_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "business_id", "user_id", name="business_member_business_user_unique"
        ),
        CheckConstraint(
            "role IN ('owner', 'approver', 'analyst')",
            name="business_member_role_check",
        ),
        Index("business_member_business_id_idx", "business_id"),
        Index("business_member_user_id_idx", "user_id"),
    )

    business: Mapped["BusinessModel"] = relationship(back_populates="members")
    user: Mapped["UserModel"] = relationship(back_populates="memberships")


# =========================================================================== #
#  BUSINESS (3 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 3. business — multi-tenancy root
# --------------------------------------------------------------------------- #
class BusinessModel(Base):
    __tablename__ = "business"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_name: Mapped[str] = mapped_column(String(255))
    business_type: Mapped[Optional[str]] = mapped_column(Text)
    interswitch_merchant_id: Mapped[Optional[str]] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        Index("business_is_active_idx", "is_active"),
    )

    members: Mapped[list["BusinessMemberModel"]] = relationship(
        back_populates="business",
    )
    config: Mapped[Optional["BusinessConfigModel"]] = relationship(
        back_populates="business",
        uselist=False,
    )
    invitations: Mapped[list["InvitationModel"]] = relationship(
        back_populates="business",
    )
    agent_runs: Mapped[list["AgentRunModel"]] = relationship(
        back_populates="business",
    )


# --------------------------------------------------------------------------- #
# 4. business_config — merged onboarding + financial profile + preferences
# --------------------------------------------------------------------------- #
class BusinessConfigModel(Base):
    __tablename__ = "business_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        unique=True,
    )
    # Onboarding
    onboarding_step: Mapped[str] = mapped_column(
        Text, server_default=text("'not_started'")
    )
    onboarding_completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    # Financial profile
    monthly_txn_volume_range: Mapped[Optional[str]] = mapped_column(String(50))
    avg_monthly_payouts_range: Mapped[Optional[str]] = mapped_column(String(50))
    primary_bank: Mapped[Optional[str]] = mapped_column(String(100))
    primary_use_cases: Mapped[Optional[list]] = mapped_column(JSONB)
    risk_appetite: Mapped[Optional[str]] = mapped_column(Text)
    default_risk_tolerance: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), server_default=text("0.3500")
    )
    default_budget_cap: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    # Preferences as JSONB
    preferences: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "onboarding_step IN ('not_started', 'business_profile', "
            "'financial_setup', 'team_invite', 'complete')",
            name="business_config_onboarding_step_check",
        ),
        CheckConstraint(
            "risk_appetite IN ('conservative', 'moderate', 'aggressive')",
            name="business_config_risk_appetite_check",
        ),
    )

    business: Mapped["BusinessModel"] = relationship(back_populates="config")


# --------------------------------------------------------------------------- #
# 5. invitation — team invite lifecycle
# --------------------------------------------------------------------------- #
class InvitationModel(Base):
    __tablename__ = "invitation"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
    )
    invited_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
    )
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    accepted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('approver', 'analyst')",
            name="invitation_role_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'expired', 'revoked')",
            name="invitation_status_check",
        ),
        Index("invitation_business_id_idx", "business_id"),
        Index("invitation_email_idx", "email"),
        Index("invitation_expires_at_idx", "expires_at"),
        Index("invitation_invited_by_idx", "invited_by"),
    )

    business: Mapped["BusinessModel"] = relationship(back_populates="invitations")


# =========================================================================== #
#  REFERENCE DATA (1 table)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 6. institution — Interswitch bank code cache (enhanced)
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
    short_name: Mapped[Optional[str]] = mapped_column(String(50))
    nip_code: Mapped[Optional[str]] = mapped_column(String(10))
    cbn_code: Mapped[Optional[str]] = mapped_column(String(10))
    institution_type: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    raw_response: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "institution_type IN ('bank', 'mobile_money', 'microfinance', 'other')",
            name="institution_type_check",
        ),
    )

    payout_candidates: Mapped[list["PayoutCandidateModel"]] = relationship(
        back_populates="institution",
    )


# =========================================================================== #
#  AGENT PIPELINE (3 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 7. agent_run — run lifecycle with multi-tenancy
# --------------------------------------------------------------------------- #
class AgentRunModel(Base):
    __tablename__ = "agent_run"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="RESTRICT"),
    )
    objective: Mapped[str] = mapped_column(Text)
    constraints: Mapped[Optional[str]] = mapped_column(Text)
    risk_tolerance: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), server_default=text("0.3500")
    )
    budget_cap: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    merchant_id: Mapped[str] = mapped_column(String(50))
    date_from: Mapped[Optional[date]] = mapped_column(Date)
    date_to: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    plan_graph: Mapped[Optional[dict]] = mapped_column(JSONB)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    cancelled_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
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
            "risk_tolerance >= 0.0000 AND risk_tolerance <= 1.0000",
            name="agent_run_risk_tolerance_check",
        ),
        CheckConstraint("budget_cap >= 0", name="agent_run_budget_cap_check"),
        CheckConstraint(
            "status IN ('pending', 'planning', 'reconciling', 'scoring', "
            "'forecasting', 'awaiting_approval', 'executing', "
            "'completed', 'completed_with_errors', 'failed', 'cancelled')",
            name="agent_run_status_check",
        ),
        Index("agent_run_status_idx", "status"),
        Index("agent_run_business_id_idx", "business_id"),
        Index("agent_run_created_by_idx", "created_by"),
        Index("agent_run_approved_by_idx", "approved_by"),
        Index("agent_run_cancelled_by_idx", "cancelled_by"),
        Index(
            "agent_run_business_id_created_at_idx",
            "business_id",
            text("created_at DESC"),
        ),
    )

    business: Mapped["BusinessModel"] = relationship(back_populates="agent_runs")
    creator: Mapped["UserModel"] = relationship(
        back_populates="created_runs",
        foreign_keys=[created_by],
    )
    approver: Mapped[Optional["UserModel"]] = relationship(
        back_populates="approved_runs",
        foreign_keys=[approved_by],
    )
    run_steps: Mapped[list["RunStepModel"]] = relationship(
        back_populates="agent_run",
    )
    run_events: Mapped[list["RunEventModel"]] = relationship(
        back_populates="agent_run",
    )
    reconciled_transactions: Mapped[list["ReconciledTransactionModel"]] = relationship(
        back_populates="agent_run",
    )
    payout_batches: Mapped[list["PayoutBatchModel"]] = relationship(
        back_populates="agent_run",
    )
    payout_candidates: Mapped[list["PayoutCandidateModel"]] = relationship(
        back_populates="agent_run",
    )
    forecast_result: Mapped[Optional["ForecastResultModel"]] = relationship(
        back_populates="agent_run",
        uselist=False,
    )
    audit_logs: Mapped[list["AuditLogModel"]] = relationship(
        back_populates="agent_run",
    )
    api_call_logs: Mapped[list["ApiCallLogModel"]] = relationship(
        back_populates="agent_run",
    )


# --------------------------------------------------------------------------- #
# 8. run_step — ordered agent steps with progress
# --------------------------------------------------------------------------- #
class RunStepModel(Base):
    __tablename__ = "run_step"

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
    progress_pct: Mapped[Optional[int]] = mapped_column(SmallInteger)
    input_data: Mapped[Optional[dict]] = mapped_column(JSONB)
    output_data: Mapped[Optional[dict]] = mapped_column(JSONB)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id", "step_order", name="run_step_run_id_step_order_unique"
        ),
        CheckConstraint(
            "agent_type IN ('planner', 'reconciliation', 'risk', "
            "'forecast', 'execution', 'audit')",
            name="run_step_agent_type_check",
        ),
        CheckConstraint("step_order >= 0", name="run_step_step_order_check"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped')",
            name="run_step_status_check",
        ),
        Index("run_step_run_id_idx", "run_id"),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(back_populates="run_steps")
    run_events: Mapped[list["RunEventModel"]] = relationship(
        back_populates="run_step",
    )
    audit_logs: Mapped[list["AuditLogModel"]] = relationship(
        back_populates="run_step",
    )
    api_call_logs: Mapped[list["ApiCallLogModel"]] = relationship(
        back_populates="run_step",
    )


# --------------------------------------------------------------------------- #
# 9. run_event — SSE replay buffer (append-only, BIGINT PK)
# --------------------------------------------------------------------------- #
class RunEventModel(Base):
    __tablename__ = "run_event"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    step_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("run_step.id", ondelete="SET NULL"),
    )
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    sequence_num: Mapped[int] = mapped_column(Integer)
    emitted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        Index("run_event_run_id_idx", "run_id"),
        Index("run_event_run_id_sequence_num_idx", "run_id", "sequence_num"),
        Index("run_event_step_id_idx", "step_id"),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(back_populates="run_events")
    run_step: Mapped[Optional["RunStepModel"]] = relationship(
        back_populates="run_events",
    )


# =========================================================================== #
#  RECONCILIATION (2 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 10. reconciled_transaction — enriched Interswitch transaction data
# --------------------------------------------------------------------------- #
class ReconciledTransactionModel(Base):
    __tablename__ = "reconciled_transaction"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
    )
    interswitch_ref: Mapped[str] = mapped_column(String(128))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    direction: Mapped[str] = mapped_column(Text)
    channel: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    narration: Mapped[Optional[str]] = mapped_column(Text)
    transaction_timestamp: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    settlement_date: Mapped[Optional[date]] = mapped_column(Date)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255))
    counterparty_bank: Mapped[Optional[str]] = mapped_column(String(100))
    has_anomaly: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    anomaly_count: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0")
    )
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "interswitch_ref",
            name="reconciled_transaction_run_ref_unique",
        ),
        CheckConstraint(
            "amount >= 0", name="reconciled_transaction_amount_check"
        ),
        CheckConstraint(
            "direction IN ('inflow', 'outflow')",
            name="reconciled_transaction_direction_check",
        ),
        CheckConstraint(
            "status IN ('SUCCESS', 'PENDING', 'FAILED', 'REVERSED')",
            name="reconciled_transaction_status_check",
        ),
        CheckConstraint(
            "channel IN ('CARD', 'TRANSFER', 'USSD', 'QR')",
            name="reconciled_transaction_channel_check",
        ),
        Index("reconciled_transaction_run_id_idx", "run_id"),
        Index("reconciled_transaction_business_id_idx", "business_id"),
        Index("reconciled_transaction_interswitch_ref_idx", "interswitch_ref"),
        Index("reconciled_transaction_status_idx", "status"),
        Index("reconciled_transaction_has_anomaly_idx", "has_anomaly"),
        Index(
            "reconciled_transaction_txn_timestamp_idx",
            text("transaction_timestamp DESC"),
        ),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(
        back_populates="reconciled_transactions",
    )
    anomalies: Mapped[list["TransactionAnomalyModel"]] = relationship(
        back_populates="transaction",
    )


# --------------------------------------------------------------------------- #
# 11. transaction_anomaly — 1:N anomalies per transaction
# --------------------------------------------------------------------------- #
class TransactionAnomalyModel(Base):
    __tablename__ = "transaction_anomaly"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    txn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reconciled_transaction.id", ondelete="CASCADE"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    anomaly_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    detected_value: Mapped[Optional[str]] = mapped_column(String(255))
    expected_range: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "severity IN ('low', 'medium', 'high')",
            name="transaction_anomaly_severity_check",
        ),
        Index("transaction_anomaly_txn_id_idx", "txn_id"),
        Index("transaction_anomaly_run_id_idx", "run_id"),
        Index("transaction_anomaly_type_idx", "anomaly_type"),
    )

    transaction: Mapped["ReconciledTransactionModel"] = relationship(
        back_populates="anomalies",
    )


# =========================================================================== #
#  RISK & FORECAST (3 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 12. payout_candidate — progressive enrichment
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
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
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
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    purpose: Mapped[Optional[str]] = mapped_column(String(255))

    risk_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    risk_reasons: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    risk_decision: Mapped[Optional[str]] = mapped_column(Text)

    lookup_status: Mapped[str] = mapped_column(
        Text, server_default=text("'pending'")
    )
    lookup_account_name: Mapped[Optional[str]] = mapped_column(String(255))
    lookup_match_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))

    approval_status: Mapped[str] = mapped_column(
        Text, server_default=text("'pending'")
    )
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    execution_status: Mapped[str] = mapped_column(
        Text, server_default=text("'not_started'")
    )
    client_reference: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    provider_reference: Mapped[Optional[str]] = mapped_column(String(100))
    transaction_reference: Mapped[Optional[str]] = mapped_column(String(255))
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
            "risk_score >= 0.0000 AND risk_score <= 1.0000",
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
        Index("payout_candidate_run_id_idx", "run_id"),
        Index("payout_candidate_business_id_idx", "business_id"),
        Index("payout_candidate_run_id_risk_decision_idx", "run_id", "risk_decision"),
        Index(
            "payout_candidate_run_id_approval_status_idx",
            "run_id",
            "approval_status",
        ),
        Index("payout_candidate_institution_code_idx", "institution_code"),
        Index("payout_candidate_batch_id_idx", "batch_id"),
        Index("payout_candidate_approved_by_idx", "approved_by"),
        Index(
            "payout_candidate_risk_reasons_idx",
            "risk_reasons",
            postgresql_using="gin",
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
    approved_by_user: Mapped[Optional["UserModel"]] = relationship(
        back_populates="approved_candidates",
    )
    risk_score_features: Mapped[Optional["RiskScoreFeatureModel"]] = relationship(
        back_populates="candidate",
        uselist=False,
    )
    customer_lookups: Mapped[list["CustomerLookupResultModel"]] = relationship(
        back_populates="candidate",
    )
    payout_executions: Mapped[list["PayoutExecutionModel"]] = relationship(
        back_populates="candidate",
    )
    approval_overrides: Mapped[list["ApprovalOverrideModel"]] = relationship(
        back_populates="candidate",
    )


# --------------------------------------------------------------------------- #
# 13. risk_score_feature — per-candidate explainability
# --------------------------------------------------------------------------- #
class RiskScoreFeatureModel(Base):
    __tablename__ = "risk_score_feature"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payout_candidate.id", ondelete="CASCADE"),
        unique=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    historical_frequency: Mapped[Optional[int]] = mapped_column(Integer)
    amount_deviation_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    avg_historical_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    duplicate_similarity_score: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(5, 4)
    )
    lookup_mismatch_flag: Mapped[Optional[bool]] = mapped_column(Boolean)
    account_anomaly_count: Mapped[Optional[int]] = mapped_column(SmallInteger)
    account_age_days: Mapped[Optional[int]] = mapped_column(Integer)
    days_since_last_payout: Mapped[Optional[int]] = mapped_column(Integer)
    amount_vs_budget_cap_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    model_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        Index("risk_score_feature_run_id_idx", "run_id"),
    )

    candidate: Mapped["PayoutCandidateModel"] = relationship(
        back_populates="risk_score_features",
    )


# --------------------------------------------------------------------------- #
# 14. forecast_result — per-run forecast with JSONB daily projections
# --------------------------------------------------------------------------- #
class ForecastResultModel(Base):
    __tablename__ = "forecast_result"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        unique=True,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
    )
    balance_today: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    total_payout_batch: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    projected_balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    feasibility: Mapped[str] = mapped_column(Text)
    stress_flag: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    inflow_7d_projected: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    outflow_7d_projected: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    net_7d_projected: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    daily_projections: Mapped[Optional[list]] = mapped_column(JSONB)
    model_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "feasibility IN ('safe', 'caution', 'block')",
            name="forecast_result_feasibility_check",
        ),
        Index("forecast_result_business_id_idx", "business_id"),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(
        back_populates="forecast_result",
    )


# =========================================================================== #
#  EXECUTION (3 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 15. payout_batch — Interswitch batch submission tracking
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
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
    )
    batch_reference: Mapped[str] = mapped_column(String(100), unique=True)
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    source_account_id: Mapped[str] = mapped_column(String(100))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
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
        Index("payout_batch_run_id_idx", "run_id"),
        Index("payout_batch_business_id_idx", "business_id"),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(back_populates="payout_batches")
    payout_candidates: Mapped[list["PayoutCandidateModel"]] = relationship(
        back_populates="payout_batch",
    )


# --------------------------------------------------------------------------- #
# 16. customer_lookup_result — per-lookup API call detail
# --------------------------------------------------------------------------- #
class CustomerLookupResultModel(Base):
    __tablename__ = "customer_lookup_result"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payout_candidate.id", ondelete="CASCADE"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    account_number: Mapped[str] = mapped_column(String(20))
    institution_code: Mapped[str] = mapped_column(String(10))
    can_credit: Mapped[Optional[bool]] = mapped_column(Boolean)
    name_returned: Mapped[Optional[str]] = mapped_column(String(255))
    similarity_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    transaction_reference: Mapped[Optional[str]] = mapped_column(String(255))
    http_status_code: Mapped[int] = mapped_column(SmallInteger)
    response_message: Mapped[Optional[str]] = mapped_column(Text)
    raw_response: Mapped[dict] = mapped_column(JSONB)
    attempt_number: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("1")
    )
    duration_ms: Mapped[int] = mapped_column(Integer)
    called_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        Index("customer_lookup_result_candidate_id_idx", "candidate_id"),
        Index("customer_lookup_result_run_id_idx", "run_id"),
    )

    candidate: Mapped["PayoutCandidateModel"] = relationship(
        back_populates="customer_lookups",
    )


# --------------------------------------------------------------------------- #
# 17. payout_execution — per-submission/poll detail (append-only, BIGINT PK)
# --------------------------------------------------------------------------- #
class PayoutExecutionModel(Base):
    __tablename__ = "payout_execution"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payout_candidate.id", ondelete="CASCADE"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    submission_type: Mapped[str] = mapped_column(Text)
    interswitch_reference: Mapped[Optional[str]] = mapped_column(String(128))
    http_status_code: Mapped[int] = mapped_column(SmallInteger)
    response_message: Mapped[Optional[str]] = mapped_column(Text)
    execution_status: Mapped[str] = mapped_column(Text)
    raw_response: Mapped[dict] = mapped_column(JSONB)
    attempt_number: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("1")
    )
    duration_ms: Mapped[int] = mapped_column(Integer)
    called_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "submission_type IN ('submission', 'status_poll')",
            name="payout_execution_submission_type_check",
        ),
        CheckConstraint(
            "execution_status IN ('pending', 'success', 'failed', 'requires_followup')",
            name="payout_execution_status_check",
        ),
        Index("payout_execution_candidate_id_idx", "candidate_id"),
        Index("payout_execution_run_id_idx", "run_id"),
        Index(
            "payout_execution_interswitch_ref_idx",
            "interswitch_reference",
            postgresql_where=text("interswitch_reference IS NOT NULL"),
        ),
        Index(
            "payout_execution_called_at_idx",
            "called_at",
            postgresql_using="brin",
        ),
    )

    candidate: Mapped["PayoutCandidateModel"] = relationship(
        back_populates="payout_executions",
    )


# =========================================================================== #
#  AUDIT & OBSERVABILITY (4 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 18. approval_override — immutable override audit trail
# --------------------------------------------------------------------------- #
class ApprovalOverrideModel(Base):
    __tablename__ = "approval_override"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payout_candidate.id", ondelete="CASCADE"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    overridden_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
    )
    original_decision: Mapped[str] = mapped_column(Text)
    original_score: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    new_decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    overridden_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "original_decision IN ('allow', 'review', 'block')",
            name="approval_override_original_decision_check",
        ),
        CheckConstraint(
            "new_decision IN ('allow', 'review', 'block')",
            name="approval_override_new_decision_check",
        ),
        Index("approval_override_candidate_id_idx", "candidate_id"),
        Index("approval_override_run_id_idx", "run_id"),
        Index("approval_override_overridden_by_idx", "overridden_by"),
    )

    candidate: Mapped["PayoutCandidateModel"] = relationship(
        back_populates="approval_overrides",
    )


# --------------------------------------------------------------------------- #
# 19. audit_log — agent action trail (BIGINT PK, BRIN-indexed, immutable)
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
        ForeignKey("run_step.id", ondelete="SET NULL"),
    )
    agent_type: Mapped[Optional[str]] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "agent_type IN ('planner', 'reconciliation', 'risk', "
            "'forecast', 'execution', 'audit')",
            name="audit_log_agent_type_check",
        ),
        Index("audit_log_run_id_idx", "run_id"),
        Index("audit_log_run_id_created_at_idx", "run_id", "created_at"),
        Index("audit_log_step_id_idx", "step_id"),
        Index(
            "audit_log_created_at_brin_idx",
            "created_at",
            postgresql_using="brin",
        ),
        Index(
            "audit_log_detail_gin_idx",
            "detail",
            postgresql_using="gin",
        ),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(back_populates="audit_logs")
    run_step: Mapped[Optional["RunStepModel"]] = relationship(
        back_populates="audit_logs",
    )


# --------------------------------------------------------------------------- #
# 20. api_call_log — Interswitch API call trace (BIGINT PK, immutable)
# --------------------------------------------------------------------------- #
class ApiCallLogModel(Base):
    __tablename__ = "api_call_log"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
    )
    step_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("run_step.id", ondelete="SET NULL"),
    )
    agent_type: Mapped[str] = mapped_column(Text)
    endpoint: Mapped[str] = mapped_column(String(255))
    http_method: Mapped[str] = mapped_column(String(8))
    http_status_code: Mapped[int] = mapped_column(SmallInteger)
    duration_ms: Mapped[int] = mapped_column(Integer)
    request_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    response_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    called_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "agent_type IN ('planner', 'reconciliation', 'risk', "
            "'forecast', 'execution', 'audit')",
            name="api_call_log_agent_type_check",
        ),
        Index("api_call_log_run_id_idx", "run_id"),
        Index("api_call_log_step_id_idx", "step_id"),
        Index(
            "api_call_log_called_at_idx",
            "called_at",
            postgresql_using="brin",
        ),
    )

    agent_run: Mapped["AgentRunModel"] = relationship(back_populates="api_call_logs")
    run_step: Mapped[Optional["RunStepModel"]] = relationship(
        back_populates="api_call_logs",
    )


# --------------------------------------------------------------------------- #
# 21. notification_outbox — async notification delivery queue (BIGINT PK)
# --------------------------------------------------------------------------- #
class NotificationOutboxModel(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
    )
    business_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="SET NULL"),
    )
    notification_type: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    subject: Mapped[Optional[str]] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    extra_data: Mapped[Optional[dict]] = mapped_column(JSONB)
    is_sent: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    sent_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    send_attempts: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0")
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    scheduled_for: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "channel IN ('email', 'in_app', 'whatsapp')",
            name="notification_outbox_channel_check",
        ),
        Index("notification_outbox_user_id_idx", "user_id"),
        Index("notification_outbox_run_id_idx", "run_id"),
        Index("notification_outbox_business_id_idx", "business_id"),
        Index(
            "notification_outbox_unsent_idx",
            "is_sent",
            postgresql_where=text("is_sent = false"),
        ),
        Index("notification_outbox_scheduled_for_idx", "scheduled_for"),
    )


# =========================================================================== #
#  Backward-compatibility aliases (for existing imports)
# =========================================================================== #
OperatorModel = UserModel
PlanStepModel = RunStepModel
TransactionModel = ReconciledTransactionModel

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
from sqlalchemy.dialects.postgresql import INET, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.infrastructure.database.base import Base


# =========================================================================== #
#  AUTH & IDENTITY (3 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 1. user — local identity record for OAuth and local-password auth
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
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    # payer | payee | admin — payee portal & auth routing (see schema redesign doc)
    account_type: Mapped[str] = mapped_column(String(20), server_default=text("'payer'"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "account_type IN ('payer', 'payee', 'admin')",
            name="user_account_type_check",
        ),
    )

    profile: Mapped[Optional["UserProfileModel"]] = relationship(  # noqa: F821
        "UserProfileModel",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    mfa: Mapped[Optional["UserMfaModel"]] = relationship(  # noqa: F821
        "UserMfaModel",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    oauth_accounts: Mapped[list["UserOauthProviderModel"]] = relationship(  # noqa: F821
        "UserOauthProviderModel",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    notification_pref_rows: Mapped[list["UserNotificationPreferenceModel"]] = relationship(  # noqa: F821
        "UserNotificationPreferenceModel",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    # --- Backward-compatible accessors (columns moved to user_profile / user_mfa) ---
    @property
    def display_name(self) -> str:
        if self.profile is not None:
            return self.profile.display_name
        return (self.email or "user").split("@")[0]

    @property
    def first_name(self) -> Optional[str]:
        return self.profile.first_name if self.profile else None

    @property
    def last_name(self) -> Optional[str]:
        return self.profile.last_name if self.profile else None

    @property
    def avatar_url(self) -> Optional[str]:
        return self.profile.avatar_url if self.profile else None

    @property
    def job_title(self) -> Optional[str]:
        return self.profile.job_title if self.profile else None

    @property
    def phone(self) -> Optional[str]:
        return self.profile.phone if self.profile else None

    @property
    def timezone(self) -> Optional[str]:
        return self.profile.timezone if self.profile else None

    @property
    def department(self) -> Optional[str]:
        return self.profile.department if self.profile else None

    @property
    def has_taken_tour(self) -> bool:
        return bool(self.profile.has_taken_tour) if self.profile else False

    @property
    def date_of_birth(self) -> Optional[date]:
        return self.profile.date_of_birth if self.profile else None

    @property
    def external_provider(self) -> Optional[str]:
        if self.oauth_accounts:
            return self.oauth_accounts[0].provider
        return None

    @property
    def totp_secret(self) -> Optional[str]:
        return self.mfa.totp_secret if self.mfa else None

    @property
    def totp_enabled_at(self) -> Optional[datetime]:
        return self.mfa.totp_enabled_at if self.mfa else None

    @property
    def backup_codes_hash(self) -> Optional[str]:
        return self.mfa.backup_codes_hash if self.mfa else None

    @property
    def totp_grace_until(self) -> Optional[datetime]:
        return self.mfa.totp_grace_until if self.mfa else None

    @property
    def approval_pin_hash(self) -> Optional[str]:
        return self.mfa.approval_pin_hash if self.mfa else None

    @property
    def notification_preferences(self) -> Optional[dict]:
        rows = self.notification_pref_rows or []
        if not rows:
            return None
        out: dict = {}
        for r in rows:
            if r.channel == "email":
                out[r.event_type] = r.is_enabled
            else:
                out[f"{r.channel}:{r.event_type}"] = r.is_enabled
        return out or None

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
# 2. invitation — pending team invite for a not-yet-registered email
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
    invited_email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(Text, server_default=text("'analyst'"))
    invited_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    token: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('approver', 'analyst')",
            name="invitation_role_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'expired')",
            name="invitation_status_check",
        ),
        Index("invitation_token_idx", "token"),
        Index("invitation_email_status_idx", "invited_email", "status"),
        Index("invitation_business_id_idx", "business_id"),
    )

    business: Mapped["BusinessModel"] = relationship()
    invited_by: Mapped[Optional["UserModel"]] = relationship(
        foreign_keys=[invited_by_user_id]
    )


# --------------------------------------------------------------------------- #
# 4. business_member — M:N user ↔ business with role
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
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
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
        Index("business_member_is_active_idx", "is_active"),
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
    # "individual" or "business" — set at onboarding, determines KYC flow + team visibility
    account_type: Mapped[str] = mapped_column(String(20), server_default=text("'business'"))
    kyc_status: Mapped[str] = mapped_column(String(20), server_default=text("'not_submitted'"))
    # Verified KYC level (0 = none, 1–3 = progressively higher)
    kyc_level: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # AI processing credits — each payout run costs 1 credit
    ai_credit_balance: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (Index("business_is_active_idx", "is_active"),)

    members: Mapped[list["BusinessMemberModel"]] = relationship(
        back_populates="business",
    )
    config: Mapped[Optional["BusinessConfigModel"]] = relationship(
        back_populates="business",
        uselist=False,
    )
    profile_row: Mapped[Optional["BusinessProfileModel"]] = relationship(  # noqa: F821
        "BusinessProfileModel",
        back_populates="business",
        uselist=False,
        cascade="all, delete-orphan",
    )
    address_row: Mapped[Optional["BusinessAddressModel"]] = relationship(  # noqa: F821
        "BusinessAddressModel",
        back_populates="business",
        uselist=False,
        cascade="all, delete-orphan",
    )
    virtual_accounts: Mapped[list["BusinessVirtualAccountModel"]] = relationship(  # noqa: F821
        "BusinessVirtualAccountModel",
        back_populates="business",
        cascade="all, delete-orphan",
    )
    payment_policy: Mapped[Optional["BusinessPaymentPolicyModel"]] = relationship(  # noqa: F821
        "BusinessPaymentPolicyModel",
        back_populates="business",
        uselist=False,
        cascade="all, delete-orphan",
    )
    security_policy: Mapped[Optional["BusinessSecurityPolicyModel"]] = relationship(  # noqa: F821
        "BusinessSecurityPolicyModel",
        back_populates="business",
        uselist=False,
        cascade="all, delete-orphan",
    )
    use_case_rows: Mapped[list["BusinessUseCaseModel"]] = relationship(  # noqa: F821
        "BusinessUseCaseModel",
        back_populates="business",
        cascade="all, delete-orphan",
    )

    agent_runs: Mapped[list["AgentRunModel"]] = relationship(
        back_populates="business",
    )

    def _primary_virtual_account(self) -> Optional["BusinessVirtualAccountModel"]:
        rows = self.virtual_accounts or []
        primary = [v for v in rows if v.is_primary]
        if primary:
            return primary[0]
        return rows[0] if rows else None

    @property
    def business_type(self) -> Optional[str]:
        return self.profile_row.business_type if self.profile_row else None

    @property
    def interswitch_merchant_id(self) -> Optional[str]:
        return self.profile_row.interswitch_merchant_id if self.profile_row else None

    @property
    def rc_number(self) -> Optional[str]:
        return self.profile_row.rc_number if self.profile_row else None

    @property
    def tax_id(self) -> Optional[str]:
        return self.profile_row.tax_id if self.profile_row else None

    @property
    def city(self) -> Optional[str]:
        return self.address_row.city if self.address_row else None

    @property
    def state(self) -> Optional[str]:
        return self.address_row.state if self.address_row else None

    @property
    def country(self) -> Optional[str]:
        return self.address_row.country if self.address_row else None

    @property
    def website(self) -> Optional[str]:
        return self.profile_row.website if self.profile_row else None

    @property
    def phone(self) -> Optional[str]:
        return self.profile_row.phone if self.profile_row else None

    @property
    def logo_url(self) -> Optional[str]:
        return self.profile_row.logo_url if self.profile_row else None

    @property
    def virtual_account_number(self) -> Optional[str]:
        va = self._primary_virtual_account()
        return va.account_number if va else None

    @property
    def virtual_account_bank(self) -> Optional[str]:
        va = self._primary_virtual_account()
        return va.bank_name if va else None

    @property
    def virtual_account_name(self) -> Optional[str]:
        va = self._primary_virtual_account()
        return va.account_name if va else None

    @property
    def virtual_account_bank_code(self) -> Optional[str]:
        va = self._primary_virtual_account()
        return va.bank_code if va else None

    @property
    def virtual_account_reference(self) -> Optional[str]:
        va = self._primary_virtual_account()
        return va.account_reference if va else None

    # --- Financial / org policy (columns moved off business_config) ---
    @property
    def monthly_txn_volume_range(self) -> Optional[str]:
        return self.payment_policy.monthly_txn_volume_range if self.payment_policy else None

    @property
    def avg_monthly_payouts_range(self) -> Optional[str]:
        return self.payment_policy.avg_monthly_payouts_range if self.payment_policy else None

    @property
    def primary_bank(self) -> Optional[str]:
        return self.payment_policy.primary_bank if self.payment_policy else None

    @property
    def risk_appetite(self) -> Optional[str]:
        return self.payment_policy.risk_appetite if self.payment_policy else None

    @property
    def default_risk_tolerance(self) -> Optional[Decimal]:
        return self.payment_policy.default_risk_tolerance if self.payment_policy else None

    @property
    def default_budget_cap(self) -> Optional[Decimal]:
        return self.payment_policy.default_budget_cap if self.payment_policy else None

    @property
    def merchant_state(self) -> Optional[str]:
        return self.payment_policy.merchant_state if self.payment_policy else None

    @property
    def daily_payout_limit(self) -> Optional[Decimal]:
        return self.payment_policy.daily_payout_limit if self.payment_policy else None

    @property
    def single_payout_cap(self) -> Optional[Decimal]:
        return self.payment_policy.single_payout_cap if self.payment_policy else None

    @property
    def risk_alert_threshold(self) -> Optional[Decimal]:
        return self.payment_policy.risk_alert_threshold if self.payment_policy else None

    @property
    def liquidity_alert_buffer(self) -> Optional[Decimal]:
        return self.payment_policy.liquidity_alert_buffer if self.payment_policy else None

    @property
    def primary_use_cases(self) -> Optional[list]:
        rows = self.use_case_rows or []
        if not rows:
            return None
        return [r.use_case for r in rows]

    @property
    def require_2fa(self) -> bool:
        return bool(self.security_policy.require_2fa) if self.security_policy else False

    @property
    def require_2fa_enforced_at(self) -> Optional[datetime]:
        return self.security_policy.require_2fa_enforced_at if self.security_policy else None


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
    # Preferences as JSONB (legacy misc; financial limits live on business_payment_policy)
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
    )

    business: Mapped["BusinessModel"] = relationship(back_populates="config")


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
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
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
    assigned_to_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
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
    # Monetization: platform fee charged at execution time
    platform_fee_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4))
    platform_fee_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
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

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
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
    anomaly_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
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
        CheckConstraint("amount >= 0", name="reconciled_transaction_amount_check"),
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
    beneficiary_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    purpose: Mapped[Optional[str]] = mapped_column(String(255))

    risk_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    risk_reasons: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    risk_decision: Mapped[Optional[str]] = mapped_column(Text)

    lookup_status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    lookup_account_name: Mapped[Optional[str]] = mapped_column(String(255))
    lookup_match_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))

    approval_status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
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
    provider: Mapped[Optional[str]] = mapped_column(String(32))
    provider_status: Mapped[Optional[str]] = mapped_column(String(32))
    monnify_reference: Mapped[Optional[str]] = mapped_column(String(100))
    monnify_status: Mapped[Optional[str]] = mapped_column(String(20))
    transaction_reference: Mapped[Optional[str]] = mapped_column(String(255))
    executed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    bank_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "payee_bank_account.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_payout_candidate_payee_bank_account",
        ),
        nullable=True,
    )
    ledger_entry_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey(
            "ledger_entry.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_payout_candidate_ledger_entry",
        ),
        nullable=True,
    )
    purpose_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

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
        Index("payout_candidate_provider_reference_idx", "provider_reference"),
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
    duplicate_similarity_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    lookup_mismatch_flag: Mapped[Optional[bool]] = mapped_column(Boolean)
    account_anomaly_count: Mapped[Optional[int]] = mapped_column(SmallInteger)
    account_age_days: Mapped[Optional[int]] = mapped_column(Integer)
    days_since_last_payout: Mapped[Optional[int]] = mapped_column(Integer)
    amount_vs_budget_cap_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    model_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (Index("risk_score_feature_run_id_idx", "run_id"),)

    candidate: Mapped["PayoutCandidateModel"] = relationship(
        back_populates="risk_score_features",
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
    accepted_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    rejected_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
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
    account_number: Mapped[str] = mapped_column(String(100))
    institution_code: Mapped[str] = mapped_column(String(10))
    can_credit: Mapped[Optional[bool]] = mapped_column(Boolean)
    name_returned: Mapped[Optional[str]] = mapped_column(String(255))
    similarity_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    transaction_reference: Mapped[Optional[str]] = mapped_column(String(255))
    http_status_code: Mapped[int] = mapped_column(SmallInteger)
    response_message: Mapped[Optional[str]] = mapped_column(Text)
    raw_response: Mapped[dict] = mapped_column(JSONB)
    attempt_number: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
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

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
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
    attempt_number: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
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
#  AUDIT & OBSERVABILITY (3 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 19. audit_log — agent action trail (BIGINT PK, BRIN-indexed, immutable)
# --------------------------------------------------------------------------- #
class AuditLogModel(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
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

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
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

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
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
    send_attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
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


# --------------------------------------------------------------------------- #
# 19. notification — user-facing in-app notification (Gap 4)
# --------------------------------------------------------------------------- #


class NotificationModel(Base):
    __tablename__ = "notification"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        index=True,
    )
    business_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(32), server_default=text("'info'"))
    resource_type: Mapped[Optional[str]] = mapped_column(String(64))
    resource_id: Mapped[Optional[str]] = mapped_column(String(64))
    is_read: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    read_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('info', 'warning', 'success', 'error')",
            name="notification_type_check",
        ),
        Index(
            "notification_user_unread_idx",
            "user_id",
            "is_read",
            postgresql_where=text("is_read = false"),
        ),
    )


# =========================================================================== #
#  CONVERSATIONAL INTENT (2 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 22. conversation — multi-turn chat session for intent extraction
# --------------------------------------------------------------------------- #
class ConversationModel(Base):
    __tablename__ = "conversation"

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
    title: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(Text, server_default=text("'gathering'"))
    current_intent: Mapped[Optional[str]] = mapped_column(String(64))
    extracted_slots: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    resolved_run_config: Mapped[Optional[dict]] = mapped_column(JSONB)
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="SET NULL"),
    )
    message_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('gathering', 'confirming', 'awaiting_approval', 'executing', 'completed', 'abandoned')",
            name="conversation_status_check",
        ),
        CheckConstraint(
            "current_intent IS NULL OR current_intent IN ("
            "'create_payout_run', 'check_run_status', 'review_candidates', "
            "'approve_reject', 'explain_system', 'view_audit', "
            "'modify_config', 'greeting', 'farewell', 'acknowledgement', "
            "'unclear')",
            name="conversation_intent_check",
        ),
        Index("conversation_business_id_idx", "business_id"),
        Index("conversation_user_id_idx", "user_id"),
        Index("conversation_status_idx", "status"),
        Index("conversation_run_id_idx", "run_id"),
        Index(
            "conversation_user_updated_idx",
            "user_id",
            text("updated_at DESC"),
        ),
    )

    messages: Mapped[list["ConversationMessageModel"]] = relationship(
        back_populates="conversation",
        order_by="ConversationMessageModel.id",
    )


# --------------------------------------------------------------------------- #
# 23. conversation_message — individual chat turn
# --------------------------------------------------------------------------- #
class ConversationMessageModel(Base):
    __tablename__ = "conversation_message"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
    )
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    intent_classification: Mapped[Optional[str]] = mapped_column(String(64))
    extracted_slots: Mapped[Optional[dict]] = mapped_column(JSONB)
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    token_usage: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="conversation_message_role_check",
        ),
        Index("conversation_message_conversation_id_idx", "conversation_id"),
        Index(
            "conversation_message_created_at_idx",
            "created_at",
            postgresql_using="brin",
        ),
    )

    conversation: Mapped["ConversationModel"] = relationship(
        back_populates="messages",
    )


# =========================================================================== #
#  MEMORY & LEARNING (3 tables) — Phase 7
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 24. run_outcome_memory — per-candidate outcomes from each run
# --------------------------------------------------------------------------- #
class RunOutcomeMemoryModel(Base):
    __tablename__ = "run_outcome_memory"

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
    candidate_account_number: Mapped[str] = mapped_column(String(20))
    candidate_bank_code: Mapped[str] = mapped_column(String(10))
    candidate_name: Mapped[Optional[str]] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    outcome: Mapped[str] = mapped_column(String(20))  # success, failed, rejected, pending
    failure_reason: Mapped[Optional[str]] = mapped_column(String(100))
    risk_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    risk_decision: Mapped[Optional[str]] = mapped_column(String(20))
    execution_duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'failed', 'rejected', 'pending', 'skipped')",
            name="run_outcome_memory_outcome_check",
        ),
        CheckConstraint(
            "risk_score IS NULL OR (risk_score >= 0.0000 AND risk_score <= 1.0000)",
            name="run_outcome_memory_risk_score_check",
        ),
        Index("run_outcome_memory_account_bank_idx", "candidate_account_number", "candidate_bank_code"),
        Index("run_outcome_memory_business_id_idx", "business_id"),
        Index("run_outcome_memory_outcome_idx", "outcome"),
        Index("run_outcome_memory_run_id_idx", "run_id"),
        Index("run_outcome_memory_created_at_idx", "created_at", postgresql_using="brin"),
    )

    agent_run: Mapped["AgentRunModel"] = relationship()
    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# 25. beneficiary_reputation — aggregated per-account reputation
# --------------------------------------------------------------------------- #
class BeneficiaryReputationModel(Base):
    __tablename__ = "beneficiary_reputation"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    account_number: Mapped[str] = mapped_column(String(20))
    bank_code: Mapped[str] = mapped_column(String(10))
    beneficiary_name: Mapped[Optional[str]] = mapped_column(String(255))
    total_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    successful_payouts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    failed_payouts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    success_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), server_default=text("0.0000")
    )
    total_amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), server_default=text("0.00")
    )
    average_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    last_outcome: Mapped[Optional[str]] = mapped_column(String(20))
    last_failure_reason: Mapped[Optional[str]] = mapped_column(String(100))
    last_payout_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    reputation_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), server_default=text("0.5000")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("account_number", "bank_code", name="beneficiary_reputation_account_bank_uq"),
        CheckConstraint(
            "success_rate >= 0.0000 AND success_rate <= 1.0000",
            name="beneficiary_reputation_success_rate_check",
        ),
        CheckConstraint(
            "reputation_score >= 0.0000 AND reputation_score <= 1.0000",
            name="beneficiary_reputation_score_check",
        ),
        Index("beneficiary_reputation_reputation_idx", "reputation_score"),
        Index("beneficiary_reputation_success_rate_idx", "success_rate"),
    )


# --------------------------------------------------------------------------- #
# 26. business_pattern_profile — learned patterns per business
# --------------------------------------------------------------------------- #
class BusinessPatternProfileModel(Base):
    __tablename__ = "business_pattern_profile"

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
    total_runs: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    total_payouts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    total_amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), server_default=text("0.00")
    )
    avg_candidates_per_run: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    avg_amount_per_candidate: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    amount_std_dev: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    amount_p25: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    amount_p50: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    amount_p75: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    amount_p95: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    overall_success_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), server_default=text("0.0000")
    )
    common_failure_reasons: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    recurring_beneficiary_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    last_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "overall_success_rate >= 0.0000 AND overall_success_rate <= 1.0000",
            name="business_pattern_profile_success_rate_check",
        ),
        Index("business_pattern_profile_business_id_idx", "business_id"),
    )

    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# 27. run_memory_digest — long-term textual recall per completed run
# --------------------------------------------------------------------------- #
class RunMemoryDigestModel(Base):
    __tablename__ = "run_memory_digest"

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
    objective: Mapped[str] = mapped_column(Text)
    digest_summary: Mapped[str] = mapped_column(Text)
    candidate_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    blocked_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        Index("run_memory_digest_business_id_idx", "business_id"),
    )

    agent_run: Mapped["AgentRunModel"] = relationship()
    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# 28. api_key — programmatic access keys for organisations
# --------------------------------------------------------------------------- #
class ApiKeyModel(Base):
    __tablename__ = "api_key"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # First 10 chars of the raw key — used as a fast lookup prefix
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # PBKDF2-SHA256 hash of the full raw key
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Fernet-encrypted raw key — allows owner to re-reveal with OTP at any time
    raw_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scopes: Mapped[list] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        Index("api_key_business_id_idx", "business_id"),
        Index("api_key_prefix_idx", "key_prefix"),
    )

    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# 29. webhook — org-level HTTP notification endpoints
# --------------------------------------------------------------------------- #
class WebhookModel(Base):
    __tablename__ = "webhook"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    events: Mapped[list] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )
    # PBKDF2 hash of the signing secret (kept for backward compat, not used for signing)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Raw whsec_... secret stored for HMAC-SHA256 payload signing
    signing_secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        Index("webhook_business_id_idx", "business_id"),
    )

    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# 29b. webhook_delivery — log of every webhook delivery attempt
# --------------------------------------------------------------------------- #
class WebhookDeliveryModel(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook.id", ondelete="CASCADE"),
        nullable=False,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    error_message: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    delivered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        Index("webhook_delivery_webhook_id_idx", "webhook_id"),
        Index("webhook_delivery_business_id_idx", "business_id"),
    )

    webhook: Mapped["WebhookModel"] = relationship()
    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# 30. approval_rule — configurable approval gates per business
# --------------------------------------------------------------------------- #
class ApprovalRuleModel(Base):
    __tablename__ = "approval_rule"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    condition: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), server_default=text("0"), nullable=False
    )
    required_approvers: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), nullable=False
    )
    approver_roles: Mapped[list] = mapped_column(
        JSONB, server_default=text("'[\"approver\"]'::jsonb"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "condition IN ('amount_above', 'risk_score_above', 'always')",
            name="approval_rule_condition_check",
        ),
        CheckConstraint(
            "required_approvers >= 1",
            name="approval_rule_min_approvers_check",
        ),
        Index("approval_rule_business_id_idx", "business_id"),
    )

    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# 31. blocklist_entry — blocked accounts / names / bank codes per business
# --------------------------------------------------------------------------- #
class BlocklistEntryModel(Base):
    __tablename__ = "blocklist_entry"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        nullable=False,
    )
    added_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, server_default=text("''"), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('account_number', 'beneficiary_name', 'bank_code')",
            name="blocklist_entry_type_check",
        ),
        Index("blocklist_entry_business_id_idx", "business_id"),
        Index("blocklist_entry_type_value_idx", "business_id", "type", "value"),
    )

    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# 32. scheduled_run — recurring payout run definitions
# --------------------------------------------------------------------------- #
class ScheduledRunModel(Base):
    __tablename__ = "scheduled_run"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    run_type: Mapped[str] = mapped_column(
        String(16), server_default=text("'recurring'"), nullable=False
    )
    # Null for one_time runs; populated for recurring runs
    cron_expression: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    frequency_label: Mapped[str] = mapped_column(String(64), nullable=False)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )
    # Tracks which next_run_at we already sent the day-before reminder for,
    # so we never send it twice for the same occurrence.
    last_reminded_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # Pre-approval gate — set when the day-before approval request is sent.
    # null → not yet requested; 'pending' → awaiting response;
    # 'approved' → owner approved; 'skipped' → owner skipped this run.
    pre_approval_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    pre_approval_token: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    pre_approval_sent_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # Stores the full payout configuration (date range, recipients, risk tolerance, etc.)
    # so the edit form can be pre-populated.
    run_config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        Index("scheduled_run_business_id_idx", "business_id"),
        Index("scheduled_run_next_run_at_idx", "next_run_at"),
    )

    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# KYC submission — business identity verification documents
# --------------------------------------------------------------------------- #
class KycSubmissionModel(Base):
    __tablename__ = "kyc_submission"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        index=True,
    )
    # status: not_submitted → pending → verified → rejected (each resubmission is a new row)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))

    # Business entity type
    # limited_company | ngo | partnership | sole_proprietorship | mda
    business_type: Mapped[Optional[str]] = mapped_column(String(50))
    registration_number: Mapped[Optional[str]] = mapped_column(String(100))
    tin_number: Mapped[Optional[str]] = mapped_column(String(50))

    # Director details (Limited Company / Sole Proprietorship)
    director_name: Mapped[Optional[str]] = mapped_column(String(255))
    director_bvn: Mapped[Optional[str]] = mapped_column(String(20))

    # NGO / Non-profit fields
    trustee_name: Mapped[Optional[str]] = mapped_column(String(255))
    trustee_bvn: Mapped[Optional[str]] = mapped_column(String(20))
    scuml_number: Mapped[Optional[str]] = mapped_column(String(100))

    # Partnership fields
    partner_names: Mapped[Optional[str]] = mapped_column(Text)  # JSON array string

    # Government / MDA fields
    authorized_officer_name: Mapped[Optional[str]] = mapped_column(String(255))
    authorized_officer_bvn: Mapped[Optional[str]] = mapped_column(String(20))

    submitted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'verified', 'rejected')",
            name="kyc_submission_status_check",
        ),
    )

    business: Mapped["BusinessModel"] = relationship()
    documents: Mapped[list["KycDocumentModel"]] = relationship(  # noqa: F821
        "KycDocumentModel",
        back_populates="submission",
        cascade="all, delete-orphan",
    )
    upgrade_requests: Mapped[list["KycUpgradeRequestModel"]] = relationship(  # noqa: F821
        "KycUpgradeRequestModel",
        back_populates="submission",
        cascade="all, delete-orphan",
    )


# --------------------------------------------------------------------------- #
# individual_kyc_submission — tiered identity verification for individual accounts
# --------------------------------------------------------------------------- #
class IndividualKycSubmissionModel(Base):
    __tablename__ = "individual_kyc_submission"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )

    # Level 1 — NIN or BVN
    level_1_type: Mapped[Optional[str]] = mapped_column(String(10))   # "nin" | "bvn"
    level_1_value: Mapped[Optional[str]] = mapped_column(String(20))
    level_1_status: Mapped[str] = mapped_column(String(20), server_default=text("'not_submitted'"))
    level_1_submitted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    level_1_verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Level 2 — proof of address (physical address + document)
    level_2_address: Mapped[Optional[str]] = mapped_column(Text)
    level_2_document_key: Mapped[Optional[str]] = mapped_column(String(512))
    level_2_status: Mapped[str] = mapped_column(String(20), server_default=text("'not_submitted'"))
    level_2_submitted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    level_2_verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Level 3 — government-issued photo ID + liveness selfie
    level_3_document_key: Mapped[Optional[str]] = mapped_column(String(512))
    level_3_selfie_key: Mapped[Optional[str]] = mapped_column(String(512))
    level_3_status: Mapped[str] = mapped_column(String(20), server_default=text("'not_submitted'"))
    level_3_submitted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    level_3_verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "level_1_type IN ('nin', 'bvn')",
            name="individual_kyc_level_1_type_check",
        ),
        CheckConstraint(
            "level_1_status IN ('not_submitted', 'pending', 'verified', 'rejected')",
            name="individual_kyc_level_1_status_check",
        ),
        CheckConstraint(
            "level_2_status IN ('not_submitted', 'pending', 'verified', 'rejected')",
            name="individual_kyc_level_2_status_check",
        ),
        CheckConstraint(
            "level_3_status IN ('not_submitted', 'pending', 'verified', 'rejected')",
            name="individual_kyc_level_3_status_check",
        ),
    )

    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# kyc_limit_tracker — tracks monthly payout usage per business for KYC limits
# --------------------------------------------------------------------------- #
class KycLimitTrackerModel(Base):
    __tablename__ = "kyc_limit_tracker"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    monthly_payout_used: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), server_default=text("0.00")
    )
    # First day of the month being tracked
    month_start: Mapped[date] = mapped_column(Date, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    business: Mapped["BusinessModel"] = relationship()


# =========================================================================== #
#  WALLET (2 tables)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# wallet — one per business, tracks prepaid balance
# --------------------------------------------------------------------------- #
class WalletModel(Base):
    __tablename__ = "wallet"

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
    balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), server_default=text("0.00")
    )
    reserved_balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), server_default=text("0.00")
    )
    currency: Mapped[str] = mapped_column(
        CHAR(3), server_default=text("'NGN'")
    )
    # Set True when a Monnify inbound credit pushes balance above the KYC tier cap.
    # Cleared when balance drops back within limit or KYC is upgraded.
    is_overlimit: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    overlimit_since: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("balance >= 0", name="wallet_balance_non_negative"),
        CheckConstraint("reserved_balance >= 0", name="wallet_reserved_non_negative"),
        CheckConstraint("reserved_balance <= balance", name="wallet_reserved_lte_balance"),
        Index("wallet_business_id_idx", "business_id"),
    )

    business: Mapped["BusinessModel"] = relationship()
    transactions: Mapped[list["WalletTransactionModel"]] = relationship(
        back_populates="wallet",
        order_by="WalletTransactionModel.created_at.desc()",
    )


# --------------------------------------------------------------------------- #
# wallet_transaction — immutable ledger of credits and debits
# --------------------------------------------------------------------------- #
class WalletTransactionModel(Base):
    __tablename__ = "wallet_transaction"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wallet.id", ondelete="CASCADE"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
    )
    # "credit" = top-up, "debit" = run spend
    type: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    # Unique key for idempotency — prevents double-processing the same operation
    reference: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    provider: Mapped[Optional[str]] = mapped_column(String(32))
    provider_reference: Mapped[Optional[str]] = mapped_column(String(255))
    # Set when this debit is linked to a specific run
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    balance_before: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    ledger_entry_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey(
            "ledger_entry.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_wallet_transaction_ledger_entry",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="wallet_tx_amount_positive"),
        CheckConstraint(
            "type IN ('credit', 'debit')",
            name="wallet_tx_type_check",
        ),
        Index("wallet_tx_wallet_id_idx", "wallet_id"),
        Index("wallet_tx_business_id_idx", "business_id"),
        Index("wallet_tx_reference_idx", "reference", unique=True),
        Index("wallet_tx_run_id_idx", "run_id"),
        Index("wallet_tx_created_at_idx", text("created_at DESC")),
    )

    wallet: Mapped["WalletModel"] = relationship(back_populates="transactions")


# --------------------------------------------------------------------------- #
# ai_credit_transaction — log of credit purchases and per-run debits
# --------------------------------------------------------------------------- #
class AiCreditTransactionModel(Base):
    __tablename__ = "ai_credit_transaction"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 'purchase' = org bought credits, 'debit' = run consumed 1 credit
    type: Mapped[str] = mapped_column(String(20))
    credits: Mapped[int] = mapped_column(Integer)
    description: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("type IN ('purchase', 'debit')", name="ai_credit_tx_type_check"),
        CheckConstraint("credits > 0", name="ai_credit_tx_credits_positive"),
        Index("ai_credit_tx_business_idx", "business_id"),
        Index("ai_credit_tx_created_idx", text("created_at DESC")),
    )


# --------------------------------------------------------------------------- #
# Saved recipient — persisted beneficiary address book per business
# --------------------------------------------------------------------------- #
class SavedRecipientModel(Base):
    __tablename__ = "saved_recipient"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    account_number: Mapped[str] = mapped_column(String(32), nullable=False)
    institution_code: Mapped[str] = mapped_column(String(16), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    payment_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    last_paid_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        Index("saved_recipient_business_id_idx", "business_id"),
        Index(
            "saved_recipient_business_account_idx",
            "business_id",
            "account_number",
            "institution_code",
        ),
    )

    business: Mapped["BusinessModel"] = relationship()


# --------------------------------------------------------------------------- #
# payout_compliance_record — FATF Travel Rule data snapshot (Rule 16)
# --------------------------------------------------------------------------- #
class PayoutComplianceRecordModel(Base):
    __tablename__ = "payout_compliance_record"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payout_candidate.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business.id", ondelete="CASCADE"),
        nullable=False,
    )
    originator_name: Mapped[str] = mapped_column(String(255), nullable=False)
    originator_wallet_id: Mapped[str] = mapped_column(String(64), nullable=False)
    originator_bvn: Mapped[str] = mapped_column(String(20), nullable=False)
    originator_address: Mapped[str] = mapped_column(Text, nullable=False)
    beneficiary_name: Mapped[str] = mapped_column(String(255), nullable=False)
    beneficiary_account_number: Mapped[str] = mapped_column(String(20), nullable=False)
    beneficiary_bank_code: Mapped[str] = mapped_column(String(10), nullable=False)
    beneficiary_bank_name: Mapped[Optional[str]] = mapped_column(String(128))
    beneficiary_address: Mapped[Optional[str]] = mapped_column(Text)
    validation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'passed'")
    )
    blocking_reason: Mapped[Optional[str]] = mapped_column(Text)
    validated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "validation_status IN ('passed', 'blocked')",
            name="payout_compliance_record_validation_status_check",
        ),
        Index("payout_compliance_record_run_id_idx", "run_id"),
        Index("payout_compliance_record_business_id_idx", "business_id"),
    )


# =========================================================================== #
#  Backward-compatibility aliases (for existing imports)
# =========================================================================== #
OperatorModel = UserModel
PlanStepModel = RunStepModel
TransactionModel = ReconciledTransactionModel


# =========================================================================== #
#  Normalized schema additions (from schema_redesign_models)
# =========================================================================== #

# --------------------------------------------------------------------------- #
# Auth & identity (normalized)
# --------------------------------------------------------------------------- #


class UserProfileModel(Base):
    __tablename__ = "user_profile"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    display_name: Mapped[str] = mapped_column(String(100))
    first_name: Mapped[Optional[str]] = mapped_column(String(100))
    last_name: Mapped[Optional[str]] = mapped_column(String(100))
    phone: Mapped[Optional[str]] = mapped_column(String(30))
    avatar_url: Mapped[Optional[str]] = mapped_column(String(512))
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    job_title: Mapped[Optional[str]] = mapped_column(String(150))
    department: Mapped[Optional[str]] = mapped_column(String(100))
    timezone: Mapped[Optional[str]] = mapped_column(String(60), server_default=text("'Africa/Lagos'"))
    has_taken_tour: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    user: Mapped["UserModel"] = relationship("UserModel", back_populates="profile", foreign_keys=[user_id])


class UserMfaModel(Base):
    __tablename__ = "user_mfa"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    totp_secret: Mapped[Optional[str]] = mapped_column(String(64))
    totp_enabled_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    backup_codes_hash: Mapped[Optional[str]] = mapped_column(Text)
    totp_grace_until: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    approval_pin_hash: Mapped[Optional[str]] = mapped_column(String(255))
    security_version: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    user: Mapped["UserModel"] = relationship("UserModel", back_populates="mfa", foreign_keys=[user_id])


class UserOauthProviderModel(Base):
    __tablename__ = "user_oauth_provider"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="user_oauth_provider_provider_external_unique"),
        Index("user_oauth_provider_user_id_idx", "user_id"),
    )

    user: Mapped["UserModel"] = relationship("UserModel", back_populates="oauth_accounts")


class UserNotificationPreferenceModel(Base):
    __tablename__ = "user_notification_preference"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(20))
    event_type: Mapped[str] = mapped_column(String(64))
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "channel IN ('email', 'in_app', 'whatsapp')",
            name="user_notification_pref_channel_check",
        ),
        UniqueConstraint("user_id", "channel", "event_type", name="user_notification_pref_unique"),
    )

    user: Mapped["UserModel"] = relationship("UserModel", back_populates="notification_pref_rows")


class UserAuditEventModel(Base):
    __tablename__ = "user_audit_event"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    business_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[Optional[str]] = mapped_column(String(64))
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    ip_address: Mapped[Optional[str]] = mapped_column(INET)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        Index("user_audit_event_user_id_idx", "user_id"),
        Index("user_audit_event_business_id_idx", "business_id"),
        Index("user_audit_event_occurred_at_idx", "occurred_at", postgresql_using="brin"),
        Index("user_audit_event_event_type_idx", "event_type"),
    )


# --------------------------------------------------------------------------- #
# Business (normalized)
# --------------------------------------------------------------------------- #


class BusinessProfileModel(Base):
    __tablename__ = "business_profile"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE"), unique=True
    )
    business_type: Mapped[Optional[str]] = mapped_column(String(50))
    rc_number: Mapped[Optional[str]] = mapped_column(String(50))
    tax_id: Mapped[Optional[str]] = mapped_column(String(50))
    phone: Mapped[Optional[str]] = mapped_column(String(30))
    website: Mapped[Optional[str]] = mapped_column(String(255))
    logo_url: Mapped[Optional[str]] = mapped_column(String(512))
    interswitch_merchant_id: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    business: Mapped["BusinessModel"] = relationship(  # noqa: F821
        "BusinessModel", back_populates="profile_row"
    )


class BusinessAddressModel(Base):
    __tablename__ = "business_address"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE"), unique=True
    )
    street_line_1: Mapped[Optional[str]] = mapped_column(String(255))
    street_line_2: Mapped[Optional[str]] = mapped_column(String(255))
    city: Mapped[Optional[str]] = mapped_column(String(100))
    state: Mapped[Optional[str]] = mapped_column(String(100))
    country: Mapped[Optional[str]] = mapped_column(String(100), server_default=text("'Nigeria'"))
    postal_code: Mapped[Optional[str]] = mapped_column(String(20))
    is_verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    business: Mapped["BusinessModel"] = relationship(  # noqa: F821
        "BusinessModel", back_populates="address_row"
    )


class BusinessVirtualAccountModel(Base):
    __tablename__ = "business_virtual_account"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE")
    )
    account_number: Mapped[str] = mapped_column(String(20))
    account_name: Mapped[Optional[str]] = mapped_column(String(128))
    bank_name: Mapped[Optional[str]] = mapped_column(String(128))
    bank_code: Mapped[Optional[str]] = mapped_column(String(10))
    account_reference: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    provider: Mapped[str] = mapped_column(String(50), server_default=text("'monnify'"))
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("provider", "account_number", name="business_virtual_account_provider_acct_unique"),
        Index("biz_virtual_account_business_id_idx", "business_id"),
    )

    business: Mapped["BusinessModel"] = relationship(  # noqa: F821
        "BusinessModel", back_populates="virtual_accounts"
    )


class BusinessPaymentPolicyModel(Base):
    __tablename__ = "business_payment_policy"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE"), unique=True
    )
    monthly_txn_volume_range: Mapped[Optional[str]] = mapped_column(String(50))
    avg_monthly_payouts_range: Mapped[Optional[str]] = mapped_column(String(50))
    primary_bank: Mapped[Optional[str]] = mapped_column(String(100))
    risk_appetite: Mapped[Optional[str]] = mapped_column(Text)
    default_risk_tolerance: Mapped[Decimal] = mapped_column(Numeric(5, 4), server_default=text("0.3500"))
    default_budget_cap: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    daily_payout_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    single_payout_cap: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    risk_alert_threshold: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    liquidity_alert_buffer: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    merchant_state: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "risk_appetite IS NULL OR risk_appetite IN ('conservative', 'moderate', 'aggressive')",
            name="business_payment_policy_risk_appetite_check",
        ),
        CheckConstraint(
            "default_risk_tolerance >= 0 AND default_risk_tolerance <= 1",
            name="business_payment_policy_risk_tol_check",
        ),
    )

    business: Mapped["BusinessModel"] = relationship(  # noqa: F821
        "BusinessModel", back_populates="payment_policy"
    )


class BusinessSecurityPolicyModel(Base):
    __tablename__ = "business_security_policy"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE"), unique=True
    )
    require_2fa: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    require_2fa_enforced_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    session_timeout_minutes: Mapped[int] = mapped_column(Integer, server_default=text("480"))
    ip_allowlist: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    business: Mapped["BusinessModel"] = relationship(  # noqa: F821
        "BusinessModel", back_populates="security_policy"
    )


class BusinessUseCaseModel(Base):
    __tablename__ = "business_use_case"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE")
    )
    use_case: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (UniqueConstraint("business_id", "use_case", name="business_use_case_unique"),)

    business: Mapped["BusinessModel"] = relationship(  # noqa: F821
        "BusinessModel", back_populates="use_case_rows"
    )


# --------------------------------------------------------------------------- #
# KYC (normalized)
# --------------------------------------------------------------------------- #


class KycDocumentModel(Base):
    __tablename__ = "kyc_document"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kyc_submission.id", ondelete="CASCADE")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE")
    )
    document_type: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(512))
    file_name: Mapped[Optional[str]] = mapped_column(String(255))
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    uploaded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        Index("kyc_document_submission_id_idx", "submission_id"),
        Index("kyc_document_business_id_idx", "business_id"),
        Index("kyc_document_type_idx", "document_type"),
    )

    submission: Mapped["KycSubmissionModel"] = relationship(  # noqa: F821
        "KycSubmissionModel",
        back_populates="documents",
        foreign_keys="KycDocumentModel.submission_id",
    )


# --------------------------------------------------------------------------- #
# kyc_upgrade_request — Level 2 / 3 upgrade requests (reason only, no docs)
# --------------------------------------------------------------------------- #
class KycUpgradeRequestModel(Base):
    __tablename__ = "kyc_upgrade_request"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kyc_submission.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE"), nullable=False
    )
    # 2 or 3
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint("level IN (2, 3)", name="kyc_upgrade_request_level_check"),
        CheckConstraint("status IN ('pending', 'verified', 'rejected')", name="kyc_upgrade_request_status_check"),
        # One active (pending) request per level per submission
        Index("kyc_upgrade_request_submission_level_idx", "submission_id", "level"),
        Index("kyc_upgrade_request_business_id_idx", "business_id"),
    )

    submission: Mapped["KycSubmissionModel"] = relationship(
        "KycSubmissionModel", back_populates="upgrade_requests"
    )


class KycPrincipalModel(Base):
    __tablename__ = "kyc_principal"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kyc_submission.id", ondelete="CASCADE")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(50))
    full_name: Mapped[str] = mapped_column(String(255))
    bvn: Mapped[Optional[str]] = mapped_column(String(20))
    id_document_key: Mapped[Optional[str]] = mapped_column(String(512))
    scuml_number: Mapped[Optional[str]] = mapped_column(String(100))
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (Index("kyc_principal_submission_id_idx", "submission_id"),)


class KycVerificationLevelModel(Base):
    __tablename__ = "kyc_verification_level"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE")
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kyc_submission.id", ondelete="CASCADE")
    )
    level: Mapped[int] = mapped_column(SmallInteger)
    id_type: Mapped[Optional[str]] = mapped_column(String(10))
    id_value: Mapped[Optional[str]] = mapped_column(String(20))
    address: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'not_submitted'"))
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("level IN (1, 2, 3)", name="kyc_verification_level_level_check"),
        CheckConstraint(
            "id_type IS NULL OR id_type IN ('nin', 'bvn')",
            name="kyc_verification_level_id_type_check",
        ),
        CheckConstraint(
            "status IN ('not_submitted', 'pending', 'verified', 'rejected')",
            name="kyc_verification_level_status_check",
        ),
        UniqueConstraint("business_id", "level", name="kyc_verification_level_business_level_unique"),
        Index("kyc_verification_level_business_id_idx", "business_id"),
        Index("kyc_verification_level_submission_id_idx", "submission_id"),
    )


class KycTierLimitModel(Base):
    __tablename__ = "kyc_tier_limit"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_type: Mapped[str] = mapped_column(String(20))
    kyc_level: Mapped[int] = mapped_column(SmallInteger)
    single_txn_limit: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    monthly_limit: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    wallet_balance_limit: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    effective_from: Mapped[date] = mapped_column(Date, server_default=text("CURRENT_DATE"))
    effective_to: Mapped[Optional[date]] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("account_type IN ('individual', 'business')", name="kyc_tier_limit_account_type_check"),
        UniqueConstraint("account_type", "kyc_level", "effective_from", name="kyc_tier_limit_effective_unique"),
    )


# --------------------------------------------------------------------------- #
# Payee portal
# --------------------------------------------------------------------------- #


class PayeeProfileModel(Base):
    __tablename__ = "payee_profile"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    display_name: Mapped[str] = mapped_column(String(100))
    business_name: Mapped[Optional[str]] = mapped_column(String(255))
    tier: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    kyc_status: Mapped[str] = mapped_column(String(20), server_default=text("'not_verified'"))
    id_type: Mapped[Optional[str]] = mapped_column(String(10))
    id_value_hash: Mapped[Optional[str]] = mapped_column(String(255))
    id_verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    invoice_prefix: Mapped[Optional[str]] = mapped_column(String(10))
    total_received: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("tier IN (1, 2, 3)", name="payee_profile_tier_check"),
        CheckConstraint(
            "kyc_status IN ('not_verified', 'pending', 'verified')",
            name="payee_profile_kyc_status_check",
        ),
        CheckConstraint("id_type IS NULL OR id_type IN ('nin', 'bvn')", name="payee_profile_id_type_check"),
    )


class PayeeBankAccountModel(Base):
    __tablename__ = "payee_bank_account"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_number: Mapped[str] = mapped_column(String(20))
    institution_code: Mapped[str] = mapped_column(
        String(10), ForeignKey("institution.institution_code", ondelete="RESTRICT")
    )
    account_name: Mapped[Optional[str]] = mapped_column(String(255))
    is_bav_verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    bav_verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    bav_match_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    payee_profile_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_profile.id", ondelete="SET NULL"), nullable=True
    )
    country_code: Mapped[str] = mapped_column(CHAR(2), server_default=text("'NG'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("account_number", "institution_code", name="payee_bank_account_acct_inst_unique"),
        Index("payee_bank_account_account_number_idx", "account_number"),
        Index("payee_bank_account_payee_profile_idx", "payee_profile_id"),
    )


class PayeePayerRelationshipModel(Base):
    __tablename__ = "payee_payer_relationship"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    payee_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_profile.id", ondelete="CASCADE")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE")
    )
    saved_recipient_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("saved_recipient.id", ondelete="SET NULL"), nullable=True
    )
    first_payment_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    last_payment_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    total_received: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    payment_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    share_schedule: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("payee_profile_id", "business_id", name="payee_payer_rel_unique"),
        Index("payee_payer_rel_payee_idx", "payee_profile_id"),
        Index("payee_payer_rel_business_idx", "business_id"),
    )


# --------------------------------------------------------------------------- #
# Ledger & wallet reservation
# --------------------------------------------------------------------------- #


class LedgerEntryModel(Base):
    __tablename__ = "ledger_entry"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    internal_reference: Mapped[str] = mapped_column(String(100), unique=True)
    client_reference: Mapped[Optional[str]] = mapped_column(String(100))
    provider_reference: Mapped[Optional[str]] = mapped_column(String(100))
    session_reference: Mapped[Optional[str]] = mapped_column(String(100))
    entry_type: Mapped[str] = mapped_column(String(50))
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    direction: Mapped[str] = mapped_column(String(6))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    failure_reason: Mapped[Optional[str]] = mapped_column(Text)
    originator_type: Mapped[str] = mapped_column(String(20))
    originator_business_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="RESTRICT"), nullable=True
    )
    originator_name: Mapped[Optional[str]] = mapped_column(String(255))
    originator_account_number: Mapped[Optional[str]] = mapped_column(String(20))
    originator_bank_name: Mapped[Optional[str]] = mapped_column(String(128))
    originator_bank_code: Mapped[Optional[str]] = mapped_column(String(10))
    beneficiary_type: Mapped[str] = mapped_column(String(20))
    beneficiary_bank_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_bank_account.id", ondelete="SET NULL"), nullable=True
    )
    beneficiary_payee_profile_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_profile.id", ondelete="SET NULL"), nullable=True
    )
    beneficiary_name: Mapped[Optional[str]] = mapped_column(String(255))
    beneficiary_account_number: Mapped[Optional[str]] = mapped_column(String(20))
    beneficiary_bank_name: Mapped[Optional[str]] = mapped_column(String(128))
    beneficiary_bank_code: Mapped[Optional[str]] = mapped_column(String(10))
    narration: Mapped[Optional[str]] = mapped_column(Text)
    internal_narration: Mapped[Optional[str]] = mapped_column(Text)
    business_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="RESTRICT"), nullable=True
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True
    )
    purpose_code: Mapped[Optional[str]] = mapped_column(String(10))
    source_table: Mapped[Optional[str]] = mapped_column(String(64))
    source_id: Mapped[Optional[str]] = mapped_column(String(64))
    initiated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    value_date: Mapped[Optional[date]] = mapped_column(Date)
    settlement_date: Mapped[Optional[date]] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("gross_amount > 0", name="ledger_entry_gross_positive"),
        CheckConstraint("direction IN ('credit', 'debit')", name="ledger_entry_direction_check"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'reversed')",
            name="ledger_entry_status_check",
        ),
        CheckConstraint(
            "originator_type IN ('business', 'external_bank', 'system', 'individual')",
            name="ledger_entry_originator_type_check",
        ),
        CheckConstraint(
            "beneficiary_type IN ('payee', 'business', 'system')",
            name="ledger_entry_beneficiary_type_check",
        ),
        Index("ledger_entry_business_id_idx", "business_id"),
        Index("ledger_entry_run_id_idx", "run_id"),
        Index("ledger_entry_entry_type_idx", "entry_type"),
        Index("ledger_entry_status_idx", "status"),
        Index("ledger_entry_initiated_at_idx", "initiated_at", postgresql_using="brin"),
    )


class WalletReservationModel(Base):
    __tablename__ = "wallet_reservation"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallet.id", ondelete="CASCADE")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE"), unique=True
    )
    reserved_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    settled_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    released_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'active'"))
    reserved_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    settled_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    ledger_reserve_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("ledger_entry.id", ondelete="SET NULL"), nullable=True
    )
    ledger_settle_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("ledger_entry.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("reserved_amount > 0", name="wallet_reservation_reserved_positive"),
        CheckConstraint(
            "status IN ('active', 'settled', 'cancelled')",
            name="wallet_reservation_status_check",
        ),
        Index("wallet_reservation_business_id_idx", "business_id"),
        Index("wallet_reservation_status_idx", "status"),
    )


class PlatformFeeTransactionModel(Base):
    __tablename__ = "platform_fee_transaction"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_run.id", ondelete="RESTRICT")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="RESTRICT")
    )
    fee_type: Mapped[str] = mapped_column(String(50), server_default=text("'platform_fee'"))
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    payout_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    min_fee_applied: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    wallet_tx_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallet_transaction.id", ondelete="RESTRICT"), nullable=True
    )
    ledger_entry_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("ledger_entry.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        Index("platform_fee_tx_run_id_idx", "run_id"),
        Index("platform_fee_tx_business_id_idx", "business_id"),
        Index("platform_fee_tx_created_at_idx", "created_at", postgresql_using="brin"),
    )


# --------------------------------------------------------------------------- #
# Compliance (NDPC / CBN)
# --------------------------------------------------------------------------- #


class ConsentRecordModel(Base):
    __tablename__ = "consent_record"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    payee_profile_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_profile.id", ondelete="SET NULL"), nullable=True
    )
    purpose: Mapped[str] = mapped_column(String(100))
    policy_version: Mapped[str] = mapped_column(String(20))
    is_granted: Mapped[bool] = mapped_column(Boolean)
    ip_address: Mapped[Optional[str]] = mapped_column(INET)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    granted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "user_id IS NOT NULL OR payee_profile_id IS NOT NULL",
            name="consent_record_subject_check",
        ),
        Index("consent_record_user_id_idx", "user_id"),
        Index("consent_record_payee_profile_id_idx", "payee_profile_id"),
    )


class SuspiciousActivityReportModel(Base):
    __tablename__ = "suspicious_activity_report"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="RESTRICT")
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payout_candidate.id", ondelete="SET NULL"), nullable=True
    )
    report_type: Mapped[str] = mapped_column(String(20), server_default=text("'SAR'"))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'draft'"))
    description: Mapped[str] = mapped_column(Text)
    submitted_to: Mapped[Optional[str]] = mapped_column(String(100), server_default=text("'NFIU'"))
    submitted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    nfiu_reference: Mapped[Optional[str]] = mapped_column(String(100))
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("report_type IN ('SAR', 'STR', 'CTR')", name="sar_report_type_check"),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'acknowledged')",
            name="sar_status_check",
        ),
    )


class DataSubjectRequestModel(Base):
    __tablename__ = "data_subject_request"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    payee_profile_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_profile.id", ondelete="SET NULL"), nullable=True
    )
    request_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'open'"))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "request_type IN ('access', 'erasure', 'rectification', 'portability')",
            name="dsr_type_check",
        ),
        CheckConstraint(
            "status IN ('open', 'in_progress', 'completed', 'rejected')",
            name="dsr_status_check",
        ),
        CheckConstraint(
            "user_id IS NOT NULL OR payee_profile_id IS NOT NULL",
            name="dsr_subject_check",
        ),
    )


class DataProcessingRecordModel(Base):
    __tablename__ = "data_processing_record"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    purpose: Mapped[str] = mapped_column(String(200))
    data_categories: Mapped[str] = mapped_column(Text)
    retention_policy: Mapped[str] = mapped_column(Text)
    legal_basis: Mapped[str] = mapped_column(String(64))
    effective_from: Mapped[date] = mapped_column(Date, server_default=text("CURRENT_DATE"))
    effective_to: Mapped[Optional[date]] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))


# --------------------------------------------------------------------------- #
# Payee invoicing
# --------------------------------------------------------------------------- #


class InvoiceModel(Base):
    __tablename__ = "invoice"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    payee_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_profile.id", ondelete="CASCADE")
    )
    invoice_number: Mapped[str] = mapped_column(String(50), unique=True)
    payer_business_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="SET NULL"), nullable=True
    )
    external_payer_name: Mapped[Optional[str]] = mapped_column(String(255))
    external_payer_email: Mapped[Optional[str]] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default=text("0.00"))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'draft'"))
    issue_date: Mapped[date] = mapped_column(Date, server_default=text("CURRENT_DATE"))
    due_date: Mapped[date] = mapped_column(Date)
    paid_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    payout_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payout_candidate.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)
    payment_terms: Mapped[Optional[str]] = mapped_column(Text)
    public_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    is_recurring: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    recurrence_rule: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'sent', 'viewed', 'partially_paid', "
            "'paid', 'overdue', 'cancelled', 'voided')",
            name="invoice_status_check",
        ),
        Index("invoice_payee_profile_id_idx", "payee_profile_id"),
        Index("invoice_payer_business_id_idx", "payer_business_id"),
        Index("invoice_status_idx", "status"),
        Index("invoice_due_date_idx", "due_date"),
        Index("invoice_public_token_idx", "public_token"),
    )


class InvoiceLineItemModel(Base):
    __tablename__ = "invoice_line_item"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice.id", ondelete="CASCADE")
    )
    description: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), server_default=text("1"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (Index("invoice_line_item_invoice_id_idx", "invoice_id"),)


class InvoiceActivityModel(Base):
    __tablename__ = "invoice_activity"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(50))
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (Index("invoice_activity_invoice_id_idx", "invoice_id"),)


class PaymentRequestModel(Base):
    __tablename__ = "payment_request"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    payee_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_profile.id", ondelete="CASCADE")
    )
    payer_business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="CASCADE")
    )
    bank_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_bank_account.id", ondelete="CASCADE")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    payout_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payout_candidate.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("amount > 0", name="payment_request_amount_positive"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'paid', 'rejected', 'cancelled', 'expired')",
            name="payment_request_status_check",
        ),
        Index("payment_request_payee_profile_id_idx", "payee_profile_id"),
        Index("payment_request_payer_business_id_idx", "payer_business_id"),
        Index("payment_request_status_idx", "status"),
    )


class IncomeStatementModel(Base):
    __tablename__ = "income_statement"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    payee_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_profile.id", ondelete="CASCADE")
    )
    period_type: Mapped[str] = mapped_column(String(10))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    total_received: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    payer_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    payment_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    document_key: Mapped[Optional[str]] = mapped_column(String(512))
    generated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("period_type IN ('monthly', 'annual')", name="income_stmt_period_type_check"),
        UniqueConstraint(
            "payee_profile_id", "period_type", "period_start", name="income_stmt_period_unique"
        ),
        Index("income_statement_payee_profile_id_idx", "payee_profile_id"),
    )


class PayeePaymentReceiptModel(Base):
    __tablename__ = "payee_payment_receipt"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    payee_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_profile.id", ondelete="RESTRICT")
    )
    payout_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payout_candidate.id", ondelete="RESTRICT")
    )
    bank_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payee_bank_account.id", ondelete="RESTRICT")
    )
    payer_business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business.id", ondelete="RESTRICT")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'NGN'"))
    purpose: Mapped[Optional[str]] = mapped_column(String(255))
    provider_reference: Mapped[Optional[str]] = mapped_column(String(100))
    receipt_number: Mapped[Optional[str]] = mapped_column(String(50), unique=True)
    document_key: Mapped[Optional[str]] = mapped_column(String(512))
    paid_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

    __table_args__ = (
        Index("payee_payment_receipt_payee_profile_idx", "payee_profile_id"),
        Index("payee_payment_receipt_paid_at_idx", "paid_at", postgresql_using="brin"),
    )

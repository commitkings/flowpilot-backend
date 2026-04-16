"""Add KYC tiers, account type, DOB, individual KYC submission, and limit tracker.

Revision ID: aa0416200006
Revises: aa0416200005
Create Date: 2026-04-16 20:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "aa0416200006"
down_revision = "aa0416200005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. user: add date_of_birth ───────────────────────────────────────────
    op.add_column("user", sa.Column("date_of_birth", sa.Date(), nullable=True))

    # ── 2. business: add account_type + kyc_level ────────────────────────────
    op.add_column(
        "business",
        sa.Column(
            "account_type",
            sa.String(20),
            nullable=False,
            server_default="business",
        ),
    )
    op.add_column(
        "business",
        sa.Column(
            "kyc_level",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "business_account_type_check",
        "business",
        "account_type IN ('individual', 'business')",
    )
    op.create_check_constraint(
        "business_kyc_level_check",
        "business",
        "kyc_level BETWEEN 0 AND 3",
    )

    # ── 3. individual_kyc_submission ─────────────────────────────────────────
    op.create_table(
        "individual_kyc_submission",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "business_id",
            UUID(as_uuid=True),
            sa.ForeignKey("business.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        # Level 1 — NIN or BVN
        sa.Column("level_1_type", sa.String(10), nullable=True),  # "nin" | "bvn"
        sa.Column("level_1_value", sa.String(20), nullable=True),
        sa.Column(
            "level_1_status",
            sa.String(20),
            nullable=False,
            server_default="not_submitted",
        ),
        sa.Column("level_1_submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("level_1_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Level 2 — address + proof of address doc
        sa.Column("level_2_address", sa.Text(), nullable=True),
        sa.Column("level_2_document_key", sa.String(512), nullable=True),
        sa.Column(
            "level_2_status",
            sa.String(20),
            nullable=False,
            server_default="not_submitted",
        ),
        sa.Column("level_2_submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("level_2_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Level 3 — government-issued photo ID (image or PDF)
        sa.Column("level_3_document_key", sa.String(512), nullable=True),
        sa.Column(
            "level_3_status",
            sa.String(20),
            nullable=False,
            server_default="not_submitted",
        ),
        sa.Column("level_3_submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("level_3_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "level_1_type IN ('nin', 'bvn')",
            name="individual_kyc_level_1_type_check",
        ),
        sa.CheckConstraint(
            "level_1_status IN ('not_submitted', 'pending', 'verified', 'rejected')",
            name="individual_kyc_level_1_status_check",
        ),
        sa.CheckConstraint(
            "level_2_status IN ('not_submitted', 'pending', 'verified', 'rejected')",
            name="individual_kyc_level_2_status_check",
        ),
        sa.CheckConstraint(
            "level_3_status IN ('not_submitted', 'pending', 'verified', 'rejected')",
            name="individual_kyc_level_3_status_check",
        ),
    )
    op.create_index(
        "individual_kyc_business_id_idx", "individual_kyc_submission", ["business_id"]
    )

    # ── 4. kyc_limit_tracker ─────────────────────────────────────────────────
    op.create_table(
        "kyc_limit_tracker",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "business_id",
            UUID(as_uuid=True),
            sa.ForeignKey("business.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column(
            "monthly_payout_used",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="0.00",
        ),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "kyc_limit_tracker_business_id_idx", "kyc_limit_tracker", ["business_id"]
    )


def downgrade() -> None:
    op.drop_table("kyc_limit_tracker")
    op.drop_table("individual_kyc_submission")
    op.drop_constraint("business_kyc_level_check", "business", type_="check")
    op.drop_constraint("business_account_type_check", "business", type_="check")
    op.drop_column("business", "kyc_level")
    op.drop_column("business", "account_type")
    op.drop_column("user", "date_of_birth")

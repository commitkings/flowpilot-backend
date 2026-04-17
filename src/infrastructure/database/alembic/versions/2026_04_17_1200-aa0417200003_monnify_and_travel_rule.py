"""add monnify fields and travel rule compliance table

Revision ID: aa0417200003
Revises: aa0417200002
Create Date: 2026-04-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa0417200003"
down_revision: str | None = "aa0417200002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("business", sa.Column("virtual_account_bank_code", sa.String(length=10), nullable=True))
    op.add_column("business", sa.Column("virtual_account_reference", sa.String(length=100), nullable=True))

    op.add_column("payout_candidate", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column("payout_candidate", sa.Column("provider_status", sa.String(length=32), nullable=True))
    op.add_column("payout_candidate", sa.Column("monnify_reference", sa.String(length=100), nullable=True))
    op.add_column("payout_candidate", sa.Column("monnify_status", sa.String(length=20), nullable=True))
    op.create_index("payout_candidate_provider_reference_idx", "payout_candidate", ["provider_reference"], unique=False)

    op.add_column("wallet_transaction", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column("wallet_transaction", sa.Column("provider_reference", sa.String(length=255), nullable=True))

    op.create_table(
        "payout_compliance_record",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("originator_name", sa.String(length=255), nullable=False),
        sa.Column("originator_wallet_id", sa.String(length=64), nullable=False),
        sa.Column("originator_bvn", sa.String(length=20), nullable=False),
        sa.Column("originator_address", sa.Text(), nullable=False),
        sa.Column("beneficiary_name", sa.String(length=255), nullable=False),
        sa.Column("beneficiary_account_number", sa.String(length=20), nullable=False),
        sa.Column("beneficiary_bank_code", sa.String(length=10), nullable=False),
        sa.Column("beneficiary_bank_name", sa.String(length=128), nullable=True),
        sa.Column("beneficiary_address", sa.Text(), nullable=True),
        sa.Column("validation_status", sa.String(length=20), server_default=sa.text("'passed'"), nullable=False),
        sa.Column("blocking_reason", sa.Text(), nullable=True),
        sa.Column("validated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "validation_status IN ('passed', 'blocked')",
            name="payout_compliance_record_validation_status_check",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["business.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["payout_candidate.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id"),
    )
    op.create_index("payout_compliance_record_run_id_idx", "payout_compliance_record", ["run_id"], unique=False)
    op.create_index("payout_compliance_record_business_id_idx", "payout_compliance_record", ["business_id"], unique=False)


def downgrade() -> None:
    op.drop_index("payout_compliance_record_business_id_idx", table_name="payout_compliance_record")
    op.drop_index("payout_compliance_record_run_id_idx", table_name="payout_compliance_record")
    op.drop_table("payout_compliance_record")

    op.drop_column("wallet_transaction", "provider_reference")
    op.drop_column("wallet_transaction", "provider")

    op.drop_index("payout_candidate_provider_reference_idx", table_name="payout_candidate")
    op.drop_column("payout_candidate", "monnify_status")
    op.drop_column("payout_candidate", "monnify_reference")
    op.drop_column("payout_candidate", "provider_status")
    op.drop_column("payout_candidate", "provider")

    op.drop_column("business", "virtual_account_reference")
    op.drop_column("business", "virtual_account_bank_code")

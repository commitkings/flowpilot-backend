"""monetization: ai credits + platform fee

Revision ID: aa0416200001
Revises: aa0415200001
Create Date: 2026-04-16 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa0416200001"
down_revision: str | None = "aa0415200001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. AI credit balance on business ────────────────────────────────────
    op.add_column(
        "business",
        sa.Column(
            "ai_credit_balance",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Seed 5 free credits for every existing business
    op.execute("UPDATE business SET ai_credit_balance = 5 WHERE ai_credit_balance = 0")
    # Hard guard: balance can never go below zero (atomic UPDATE ensures this in code,
    # but the constraint is a last-resort safety net)
    op.create_check_constraint(
        "business_ai_credit_balance_non_negative",
        "business",
        "ai_credit_balance >= 0",
    )

    # ── 2. AI credit transaction log ─────────────────────────────────────────
    op.create_table(
        "ai_credit_transaction",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "business_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("business.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("agent_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("type IN ('purchase', 'debit')", name="ai_credit_tx_type_check"),
        sa.CheckConstraint("credits > 0", name="ai_credit_tx_credits_positive"),
    )
    op.create_index("ai_credit_tx_business_idx", "ai_credit_transaction", ["business_id"])
    op.create_index("ai_credit_tx_created_idx", "ai_credit_transaction", ["created_at"])
    # One debit log entry per run — prevents duplicate credit charges for the same run_id
    op.create_index(
        "ai_credit_tx_run_id_debit_uniq",
        "ai_credit_transaction",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("type = 'debit' AND run_id IS NOT NULL"),
    )

    # ── 3. Platform fee columns on agent_run ─────────────────────────────────
    op.add_column(
        "agent_run",
        sa.Column("platform_fee_rate", sa.Numeric(6, 4), nullable=True),
    )
    op.add_column(
        "agent_run",
        sa.Column("platform_fee_amount", sa.Numeric(18, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_run", "platform_fee_amount")
    op.drop_column("agent_run", "platform_fee_rate")
    op.drop_index("ai_credit_tx_run_id_debit_uniq", table_name="ai_credit_transaction")
    op.drop_index("ai_credit_tx_created_idx", table_name="ai_credit_transaction")
    op.drop_index("ai_credit_tx_business_idx", table_name="ai_credit_transaction")
    op.drop_table("ai_credit_transaction")
    op.drop_constraint("business_ai_credit_balance_non_negative", "business", type_="check")
    op.drop_column("business", "ai_credit_balance")

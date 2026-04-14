"""add wallet and wallet_transaction tables

Revision ID: aa0414200009
Revises: aa0414200008
Create Date: 2026-04-14 27:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "aa0414200009"
down_revision: str | None = "aa0414200008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── wallet ────────────────────────────────────────────────────────────────
    op.create_table(
        "wallet",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "balance",
            sa.Numeric(18, 2),
            nullable=False,
            server_default=sa.text("0.00"),
        ),
        sa.Column(
            "currency",
            sa.CHAR(3),
            nullable=False,
            server_default=sa.text("'NGN'"),
        ),
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
        sa.CheckConstraint("balance >= 0", name="wallet_balance_non_negative"),
    )
    op.create_index("wallet_business_id_idx", "wallet", ["business_id"])

    # ── wallet_transaction ────────────────────────────────────────────────────
    op.create_table(
        "wallet_transaction",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wallet.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("reference", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("balance_before", sa.Numeric(18, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="wallet_tx_amount_positive"),
        sa.CheckConstraint(
            "type IN ('credit', 'debit')", name="wallet_tx_type_check"
        ),
    )
    op.create_index("wallet_tx_wallet_id_idx", "wallet_transaction", ["wallet_id"])
    op.create_index("wallet_tx_business_id_idx", "wallet_transaction", ["business_id"])
    op.create_index(
        "wallet_tx_reference_idx", "wallet_transaction", ["reference"], unique=True
    )
    op.create_index(
        "wallet_tx_run_id_idx",
        "wallet_transaction",
        ["run_id"],
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )
    op.create_index(
        "wallet_tx_created_at_idx",
        "wallet_transaction",
        [sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("wallet_tx_created_at_idx", table_name="wallet_transaction")
    op.drop_index("wallet_tx_run_id_idx", table_name="wallet_transaction")
    op.drop_index("wallet_tx_reference_idx", table_name="wallet_transaction")
    op.drop_index("wallet_tx_business_id_idx", table_name="wallet_transaction")
    op.drop_index("wallet_tx_wallet_id_idx", table_name="wallet_transaction")
    op.drop_table("wallet_transaction")
    op.drop_index("wallet_business_id_idx", table_name="wallet")
    op.drop_table("wallet")

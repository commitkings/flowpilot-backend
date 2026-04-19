"""Schema redesign phase 1: normalized tables, ledger, payee portal, additive columns.

Revision ID: aa0419200001
Revises: aa0418200001
Create Date: 2026-04-19 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "aa0419200001"
down_revision = "aa0418200001"
branch_labels = None
depends_on = None

# Tables introduced in schema_redesign_models (same names as __tablename__)
NEW_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "user_profile",
        "user_mfa",
        "user_oauth_provider",
        "user_notification_preference",
        "user_audit_event",
        "business_profile",
        "business_address",
        "business_virtual_account",
        "business_payment_policy",
        "business_security_policy",
        "business_use_case",
        "kyc_document",
        "kyc_principal",
        "kyc_verification_level",
        "kyc_tier_limit",
        "payee_profile",
        "payee_bank_account",
        "payee_payer_relationship",
        "ledger_entry",
        "wallet_reservation",
        "platform_fee_transaction",
        "consent_record",
        "suspicious_activity_report",
        "data_subject_request",
        "data_processing_record",
        "invoice",
        "invoice_line_item",
        "invoice_activity",
        "payment_request",
        "income_statement",
        "payee_payment_receipt",
    }
)


def upgrade() -> None:
    bind = op.get_bind()
    from src.infrastructure.database.base import Base

    import src.infrastructure.database.flowpilot_models  # noqa: F401 — register metadata

    for table in Base.metadata.sorted_tables:
        if table.name not in NEW_TABLE_NAMES:
            continue
        table.create(bind=bind, checkfirst=True)

    op.add_column(
        "user",
        sa.Column(
            "account_type",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'payer'"),
        ),
    )
    op.create_check_constraint(
        "user_account_type_check",
        "user",
        "account_type IN ('payer', 'payee', 'admin')",
    )

    op.add_column(
        "wallet",
        sa.Column(
            "reserved_balance",
            sa.Numeric(18, 2),
            nullable=False,
            server_default=sa.text("0.00"),
        ),
    )
    op.create_check_constraint(
        "wallet_reserved_non_negative",
        "wallet",
        "reserved_balance >= 0",
    )
    op.create_check_constraint(
        "wallet_reserved_lte_balance",
        "wallet",
        "reserved_balance <= balance",
    )

    op.add_column(
        "payout_candidate",
        sa.Column("bank_account_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "payout_candidate",
        sa.Column("ledger_entry_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "payout_candidate",
        sa.Column("purpose_code", sa.String(10), nullable=True),
    )
    op.create_foreign_key(
        "fk_payout_candidate_payee_bank_account",
        "payout_candidate",
        "payee_bank_account",
        ["bank_account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_payout_candidate_ledger_entry",
        "payout_candidate",
        "ledger_entry",
        ["ledger_entry_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "wallet_transaction",
        sa.Column("ledger_entry_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_wallet_transaction_ledger_entry",
        "wallet_transaction",
        "ledger_entry",
        ["ledger_entry_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_wallet_transaction_ledger_entry", "wallet_transaction", type_="foreignkey"
    )
    op.drop_column("wallet_transaction", "ledger_entry_id")

    op.drop_constraint(
        "fk_payout_candidate_ledger_entry", "payout_candidate", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_payout_candidate_payee_bank_account", "payout_candidate", type_="foreignkey"
    )
    op.drop_column("payout_candidate", "purpose_code")
    op.drop_column("payout_candidate", "ledger_entry_id")
    op.drop_column("payout_candidate", "bank_account_id")

    op.drop_constraint("wallet_reserved_lte_balance", "wallet", type_="check")
    op.drop_constraint("wallet_reserved_non_negative", "wallet", type_="check")
    op.drop_column("wallet", "reserved_balance")

    op.drop_constraint("user_account_type_check", "user", type_="check")
    op.drop_column("user", "account_type")

    bind = op.get_bind()
    from src.infrastructure.database.base import Base

    import src.infrastructure.database.flowpilot_models  # noqa: F401

    tables = [
        t
        for t in reversed(Base.metadata.sorted_tables)
        if t.name in NEW_TABLE_NAMES
    ]
    for table in tables:
        table.drop(bind=bind, checkfirst=True)

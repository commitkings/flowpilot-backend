"""ledger_entry: add 'individual' to originator_type check constraint

Revision ID: aa0419200007
Revises: aa0419200006
Create Date: 2026-04-19 22:00:00.000000

"""
from alembic import op

revision = "aa0419200007"
down_revision = "aa0419200006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ledger_entry_originator_type_check", "ledger_entry")
    op.create_check_constraint(
        "ledger_entry_originator_type_check",
        "ledger_entry",
        "originator_type IN ('business', 'external_bank', 'system', 'individual')",
    )


def downgrade() -> None:
    op.drop_constraint("ledger_entry_originator_type_check", "ledger_entry")
    op.create_check_constraint(
        "ledger_entry_originator_type_check",
        "ledger_entry",
        "originator_type IN ('business', 'external_bank', 'system')",
    )

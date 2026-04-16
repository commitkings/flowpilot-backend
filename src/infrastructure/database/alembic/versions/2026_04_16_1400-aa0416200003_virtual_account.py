"""business: add virtual account details assigned at onboarding

Revision ID: aa0416200003
Revises: aa0416200002
Create Date: 2026-04-16 14:00:00.000000

Each business gets a unique virtual account number on creation so they
can fund their FlowPilot wallet via bank transfer.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa0416200003"
down_revision: str | None = "aa0416200002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "business",
        sa.Column("virtual_account_number", sa.String(20), nullable=True),
    )
    op.add_column(
        "business",
        sa.Column(
            "virtual_account_bank",
            sa.String(128),
            nullable=True,
            server_default=sa.text("'FlowPilot Microfinance Bank'"),
        ),
    )
    op.add_column(
        "business",
        sa.Column("virtual_account_name", sa.String(128), nullable=True),
    )
    # Unique constraint — no two businesses share the same account number
    op.create_unique_constraint(
        "business_virtual_account_number_uq",
        "business",
        ["virtual_account_number"],
    )

    # Back-fill existing businesses with generated account numbers.
    # Strip dashes from the UUID before using it as hex — LEFT(id::text, 15)
    # would otherwise include '-' characters which are not valid hex digits.
    op.execute("""
        UPDATE business
        SET virtual_account_number = LPAD(
            (ABS(('x' || LEFT(REPLACE(id::text, '-', ''), 15))::bit(64)::bigint) % 9000000000 + 1000000000)::text,
            10, '0'
        ),
        virtual_account_name = LEFT(business_name, 30)
        WHERE virtual_account_number IS NULL
    """)


def downgrade() -> None:
    op.drop_constraint("business_virtual_account_number_uq", "business", type_="unique")
    op.drop_column("business", "virtual_account_name")
    op.drop_column("business", "virtual_account_bank")
    op.drop_column("business", "virtual_account_number")

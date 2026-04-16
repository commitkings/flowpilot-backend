"""Add approval_pin_hash to user table.

Revision ID: aa0416200007
Revises: aa0416200006
Create Date: 2026-04-16 21:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "aa0416200007"
down_revision = "aa0416200006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("approval_pin_hash", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user", "approval_pin_hash")

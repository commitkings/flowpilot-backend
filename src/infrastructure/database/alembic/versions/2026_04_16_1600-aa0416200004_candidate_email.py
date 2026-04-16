"""add beneficiary_email to payout_candidate

Revision ID: aa0416200004
Revises: aa0416200003
Create Date: 2026-04-16 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "aa0416200004"
down_revision = "aa0416200003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payout_candidate",
        sa.Column("beneficiary_email", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payout_candidate", "beneficiary_email")

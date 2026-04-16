"""Add level_3_selfie_key to individual_kyc_submission table.

Revision ID: aa0416200008
Revises: aa0416200007
Create Date: 2026-04-16 22:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "aa0416200008"
down_revision = "aa0416200007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "individual_kyc_submission",
        sa.Column("level_3_selfie_key", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("individual_kyc_submission", "level_3_selfie_key")

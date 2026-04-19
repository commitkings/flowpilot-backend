"""Add is_overlimit and overlimit_since columns to wallet table.

Revision ID: aa0419200003
Revises: aa0419200002
Create Date: 2026-04-19 18:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision = "aa0419200003"
down_revision = "aa0419200002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wallet",
        sa.Column("is_overlimit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "wallet",
        sa.Column("overlimit_since", TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wallet", "overlimit_since")
    op.drop_column("wallet", "is_overlimit")

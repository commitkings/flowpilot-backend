"""Add raw_key_encrypted to api_key table.

Revision ID: aa0417200001
Revises: aa0416200008
Create Date: 2026-04-17 00:01:00
"""

from alembic import op
import sqlalchemy as sa

revision = "aa0417200001"
down_revision = "aa0416200008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_key",
        sa.Column("raw_key_encrypted", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_key", "raw_key_encrypted")

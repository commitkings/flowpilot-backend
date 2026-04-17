"""Add run_config to scheduled_run table.

Revision ID: aa0417200003
Revises: aa0417200002
Create Date: 2026-04-17 00:03:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "aa0417200003"
down_revision = "aa0417200002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_run",
        sa.Column("run_config", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scheduled_run", "run_config")

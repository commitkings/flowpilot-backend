"""Add notification_preferences JSONB column to user table.

Revision ID: aa0418200001
Revises: aa0417200003
Create Date: 2026-04-18 00:01:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "aa0418200001"
down_revision = "aa0417200003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("notification_preferences", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user", "notification_preferences")

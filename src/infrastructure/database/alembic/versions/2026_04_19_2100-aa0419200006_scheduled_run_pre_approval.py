"""add pre_approval fields to scheduled_run for day-before approval gate

Revision ID: aa0419200006
Revises: aa0419200005
Create Date: 2026-04-19 21:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "aa0419200006"
down_revision = "aa0419200005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_run", sa.Column("pre_approval_status", sa.String(20), nullable=True))
    op.add_column("scheduled_run", sa.Column("pre_approval_token", UUID(as_uuid=True), nullable=True))
    op.add_column("scheduled_run", sa.Column("pre_approval_sent_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("scheduled_run", "pre_approval_sent_at")
    op.drop_column("scheduled_run", "pre_approval_token")
    op.drop_column("scheduled_run", "pre_approval_status")

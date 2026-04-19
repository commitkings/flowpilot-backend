"""add kyc_upgrade_request table for business Level 2/3 upgrade requests

Revision ID: aa0419200004
Revises: aa0419200003
Create Date: 2026-04-19 19:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "aa0419200004"
down_revision = "aa0419200003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kyc_upgrade_request",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("submission_id", UUID(as_uuid=True), sa.ForeignKey("kyc_submission.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_id", UUID(as_uuid=True), sa.ForeignKey("business.id", ondelete="CASCADE"), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("level IN (2, 3)", name="kyc_upgrade_request_level_check"),
        sa.CheckConstraint("status IN ('pending', 'verified', 'rejected')", name="kyc_upgrade_request_status_check"),
    )
    op.create_index("kyc_upgrade_request_submission_level_idx", "kyc_upgrade_request", ["submission_id", "level"])
    op.create_index("kyc_upgrade_request_business_id_idx", "kyc_upgrade_request", ["business_id"])


def downgrade() -> None:
    op.drop_index("kyc_upgrade_request_business_id_idx")
    op.drop_index("kyc_upgrade_request_submission_level_idx")
    op.drop_table("kyc_upgrade_request")

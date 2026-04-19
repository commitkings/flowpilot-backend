"""drop unique constraint on kyc_submission.business_id to allow submission history

Revision ID: aa0419200005
Revises: aa0419200004
Create Date: 2026-04-19 20:00:00.000000
"""
from alembic import op

revision = "aa0419200005"
down_revision = "aa0419200004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the implicit unique index created by unique=True on business_id.
    # Each resubmission after rejection now inserts a new row; the latest is
    # identified by submitted_at DESC.
    op.drop_constraint("kyc_submission_business_id_key", "kyc_submission", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("kyc_submission_business_id_key", "kyc_submission", ["business_id"])

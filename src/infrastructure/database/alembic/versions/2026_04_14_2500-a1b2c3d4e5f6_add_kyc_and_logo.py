"""add kyc_submission table, logo_url and kyc_status to business

Revision ID: aa0414200007
Revises: aa0414200006
Create Date: 2026-04-14 25:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "aa0414200007"
down_revision: str | None = "aa0414200006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add logo_url and kyc_status to business table
    op.add_column(
        "business",
        sa.Column("logo_url", sa.String(512), nullable=True),
    )
    op.add_column(
        "business",
        sa.Column(
            "kyc_status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'not_submitted'"),
        ),
    )

    # Create kyc_submission table
    op.create_table(
        "kyc_submission",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("cac_certificate_key", sa.String(512), nullable=True),
        sa.Column("tin_document_key", sa.String(512), nullable=True),
        sa.Column("director_id_key", sa.String(512), nullable=True),
        sa.Column("proof_of_address_key", sa.String(512), nullable=True),
        sa.Column("director_name", sa.String(255), nullable=True),
        sa.Column("director_bvn", sa.String(20), nullable=True),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'verified', 'rejected')",
            name="kyc_submission_status_check",
        ),
    )
    op.create_index("kyc_submission_business_id_idx", "kyc_submission", ["business_id"])


def downgrade() -> None:
    op.drop_index("kyc_submission_business_id_idx", table_name="kyc_submission")
    op.drop_table("kyc_submission")
    op.drop_column("business", "kyc_status")
    op.drop_column("business", "logo_url")

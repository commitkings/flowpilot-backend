"""kyc_submission: add missing columns (business_type, registration fields, entity-specific)

Revision ID: aa0416200005
Revises: aa0416200004
Create Date: 2026-04-16 18:00:00.000000

The original kyc_submission migration (aa0414200007) only created the base
director fields. All business-type discriminator and entity-specific columns
were added to the ORM model but never landed in the database. This migration
closes that gap.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa0416200005"
down_revision: str | None = "aa0416200004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Business-type discriminator + shared registration fields
    op.add_column("kyc_submission", sa.Column("business_type", sa.String(50), nullable=True))
    op.add_column("kyc_submission", sa.Column("registration_number", sa.String(100), nullable=True))
    op.add_column("kyc_submission", sa.Column("tin_number", sa.String(50), nullable=True))

    # NGO / Non-profit fields
    op.add_column("kyc_submission", sa.Column("trustee_name", sa.String(255), nullable=True))
    op.add_column("kyc_submission", sa.Column("trustee_bvn", sa.String(20), nullable=True))
    op.add_column("kyc_submission", sa.Column("trustee_id_key", sa.String(512), nullable=True))
    op.add_column("kyc_submission", sa.Column("scuml_number", sa.String(100), nullable=True))
    op.add_column("kyc_submission", sa.Column("scuml_letter_key", sa.String(512), nullable=True))

    # Partnership fields
    op.add_column("kyc_submission", sa.Column("partner_names", sa.Text, nullable=True))
    op.add_column("kyc_submission", sa.Column("partner_id_key", sa.String(512), nullable=True))

    # Government / MDA fields
    op.add_column("kyc_submission", sa.Column("mda_letter_key", sa.String(512), nullable=True))
    op.add_column("kyc_submission", sa.Column("authorized_officer_name", sa.String(255), nullable=True))
    op.add_column("kyc_submission", sa.Column("authorized_officer_bvn", sa.String(20), nullable=True))
    op.add_column("kyc_submission", sa.Column("authorized_officer_id_key", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("kyc_submission", "authorized_officer_id_key")
    op.drop_column("kyc_submission", "authorized_officer_bvn")
    op.drop_column("kyc_submission", "authorized_officer_name")
    op.drop_column("kyc_submission", "mda_letter_key")
    op.drop_column("kyc_submission", "partner_id_key")
    op.drop_column("kyc_submission", "partner_names")
    op.drop_column("kyc_submission", "scuml_letter_key")
    op.drop_column("kyc_submission", "scuml_number")
    op.drop_column("kyc_submission", "trustee_id_key")
    op.drop_column("kyc_submission", "trustee_bvn")
    op.drop_column("kyc_submission", "trustee_name")
    op.drop_column("kyc_submission", "tin_number")
    op.drop_column("kyc_submission", "registration_number")
    op.drop_column("kyc_submission", "business_type")

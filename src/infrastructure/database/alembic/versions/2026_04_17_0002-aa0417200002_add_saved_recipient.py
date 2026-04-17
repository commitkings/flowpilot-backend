"""Add saved_recipient table.

Revision ID: aa0417200002
Revises: aa0417200001
Create Date: 2026-04-17 00:02:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "aa0417200002"
down_revision = "aa0417200001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_recipient",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "business_id",
            UUID(as_uuid=True),
            sa.ForeignKey("business.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("account_number", sa.String(32), nullable=False),
        sa.Column("institution_code", sa.String(16), nullable=False),
        sa.Column("email", sa.String(256), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "tags",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "payment_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "last_paid_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("saved_recipient_business_id_idx", "saved_recipient", ["business_id"])
    op.create_index(
        "saved_recipient_business_account_idx",
        "saved_recipient",
        ["business_id", "account_number", "institution_code"],
    )


def downgrade() -> None:
    op.drop_index("saved_recipient_business_account_idx", table_name="saved_recipient")
    op.drop_index("saved_recipient_business_id_idx", table_name="saved_recipient")
    op.drop_table("saved_recipient")

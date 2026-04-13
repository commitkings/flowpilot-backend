"""add email_verified_at to user table

Revision ID: e9a4b3c2d1f7
Revises: d8f3a2b1c9e5
Create Date: 2026-04-13 13:00:00.000000

OTP tokens are stored in Redis with a 15-minute TTL — no DB columns needed.
Only the verified-at timestamp lives in the DB.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

# revision identifiers, used by Alembic.
revision: str = "e9a4b3c2d1f7"
down_revision: Union[str, None] = "d8f3a2b1c9e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("email_verified_at", TIMESTAMP(timezone=True), nullable=True),
    )
    # Grandfather all existing users as verified so they are not locked out
    op.execute(
        'UPDATE "user" SET email_verified_at = NOW() WHERE email_verified_at IS NULL'
    )


def downgrade() -> None:
    op.drop_column("user", "email_verified_at")

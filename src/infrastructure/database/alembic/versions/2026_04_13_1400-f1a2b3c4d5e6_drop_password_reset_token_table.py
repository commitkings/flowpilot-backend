"""drop password_reset_token table — tokens now live in Redis

Revision ID: f1a2b3c4d5e6
Revises: e9a4b3c2d1f7
Create Date: 2026-04-13 14:00:00.000000

The password_reset_token table is no longer written to. Reset tokens are
stored in Redis with a TTL matching PASSWORD_RESET_TOKEN_EXPIRY_MINUTES.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e9a4b3c2d1f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("password_reset_token")


def downgrade() -> None:
    # Recreate the table so rollback is possible
    op.create_table(
        "password_reset_token",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

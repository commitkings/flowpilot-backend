"""add 2FA / TOTP fields to user and business_config

Revision ID: a1b2c3d4e5f6
Revises: d5e6f7a8b9c0
Create Date: 2026-04-14 10:00:00.000000

Adds TOTP secret + enabled timestamp + backup codes + grace period to user.
Adds require_2fa + enforced_at to business_config.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- user table ----------------------------------------------------------
    op.add_column("user", sa.Column("totp_secret", sa.String(64), nullable=True))
    op.add_column("user", sa.Column("totp_enabled_at", TIMESTAMP(timezone=True), nullable=True))
    op.add_column("user", sa.Column("backup_codes_hash", sa.Text(), nullable=True))
    op.add_column("user", sa.Column("totp_grace_until", TIMESTAMP(timezone=True), nullable=True))

    # -- business_config table -----------------------------------------------
    op.add_column("business_config", sa.Column("require_2fa", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("business_config", sa.Column("require_2fa_enforced_at", TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("user", "totp_secret")
    op.drop_column("user", "totp_enabled_at")
    op.drop_column("user", "backup_codes_hash")
    op.drop_column("user", "totp_grace_until")
    op.drop_column("business_config", "require_2fa")
    op.drop_column("business_config", "require_2fa_enforced_at")

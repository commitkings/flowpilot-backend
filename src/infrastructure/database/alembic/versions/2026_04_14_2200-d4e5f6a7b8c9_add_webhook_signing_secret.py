"""add webhook signing_secret column

Revision ID: aa0414200004
Revises: aa0414200003
Create Date: 2026-04-14 22:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "aa0414200004"
down_revision = "aa0414200003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Store the raw whsec_... signing secret so we can compute HMAC-SHA256
    # signatures on outgoing webhook payloads. The old secret_hash column
    # (PBKDF2 one-way hash) is kept for backward compat but is no longer used
    # for signing.
    op.add_column(
        "webhook",
        sa.Column("signing_secret", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("webhook", "signing_secret")

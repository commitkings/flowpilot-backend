"""add webhook signing_secret column

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-14 22:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
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

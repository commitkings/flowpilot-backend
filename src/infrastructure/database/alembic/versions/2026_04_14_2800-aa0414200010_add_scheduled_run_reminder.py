"""add last_reminded_at to scheduled_run

Revision ID: aa0414200010
Revises: aa0414200009
Create Date: 2026-04-14 28:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa0414200010"
down_revision: str | None = "aa0414200009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduled_run",
        sa.Column("last_reminded_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scheduled_run", "last_reminded_at")

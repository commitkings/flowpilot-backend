"""add expires_at to api_key table

Revision ID: aa0414200006
Revises: aa0414200005
Create Date: 2026-04-14 24:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa0414200006"
down_revision: str | None = "aa0414200005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_key",
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_key", "expires_at")

"""add assigned_to_id to agent_run

Revision ID: aa0414200008
Revises: aa0414200007
Create Date: 2026-04-14 26:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "aa0414200008"
down_revision: str | None = "aa0414200007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_run",
        sa.Column(
            "assigned_to_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "agent_run_assigned_to_id_idx",
        "agent_run",
        ["assigned_to_id"],
        postgresql_where=sa.text("assigned_to_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("agent_run_assigned_to_id_idx", table_name="agent_run")
    op.drop_column("agent_run", "assigned_to_id")

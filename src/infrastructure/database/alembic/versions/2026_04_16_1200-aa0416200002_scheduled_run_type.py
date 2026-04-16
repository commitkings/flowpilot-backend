"""scheduled_run: add run_type column, make cron_expression nullable

Revision ID: aa0416200002
Revises: aa0416200001
Create Date: 2026-04-16 12:00:00.000000

One-time runs store the target fire time in next_run_at and have no
cron expression. Recurring runs behave as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa0416200002"
down_revision: str | None = "aa0416200001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Make cron_expression nullable — one-time runs don't have a cron
    op.alter_column(
        "scheduled_run",
        "cron_expression",
        existing_type=sa.String(128),
        nullable=True,
    )

    # 2. Add run_type with default 'recurring' so existing rows are unaffected
    op.add_column(
        "scheduled_run",
        sa.Column(
            "run_type",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'recurring'"),
        ),
    )
    op.create_check_constraint(
        "scheduled_run_type_check",
        "scheduled_run",
        "run_type IN ('recurring', 'one_time')",
    )


def downgrade() -> None:
    op.drop_constraint("scheduled_run_type_check", "scheduled_run", type_="check")
    op.drop_column("scheduled_run", "run_type")
    # Restore NOT NULL on cron_expression (fill any NULLs first)
    op.execute("UPDATE scheduled_run SET cron_expression = '' WHERE cron_expression IS NULL")
    op.alter_column(
        "scheduled_run",
        "cron_expression",
        existing_type=sa.String(128),
        nullable=False,
    )

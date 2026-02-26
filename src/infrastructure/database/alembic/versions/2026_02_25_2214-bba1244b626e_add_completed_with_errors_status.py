"""add completed_with_errors status

Revision ID: bba1244b626e
Revises: 68be94d35f2c
Create Date: 2026-02-25 22:14:15.349148

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bba1244b626e'
down_revision: Union[str, Sequence[str], None] = '68be94d35f2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add completed_with_errors to agent_run status CHECK constraint."""
    op.drop_constraint("agent_run_status_check", "agent_run", type_="check")
    op.create_check_constraint(
        "agent_run_status_check",
        "agent_run",
        "status IN ('pending', 'planning', 'reconciling', 'scoring', "
        "'forecasting', 'awaiting_approval', 'executing', "
        "'completed', 'completed_with_errors', 'failed', 'cancelled')",
    )


def downgrade() -> None:
    """Remove completed_with_errors from agent_run status CHECK constraint."""
    op.execute("UPDATE agent_run SET status = 'failed' WHERE status = 'completed_with_errors'")
    op.drop_constraint("agent_run_status_check", "agent_run", type_="check")
    op.create_check_constraint(
        "agent_run_status_check",
        "agent_run",
        "status IN ('pending', 'planning', 'reconciling', 'scoring', "
        "'forecasting', 'awaiting_approval', 'executing', "
        "'completed', 'failed', 'cancelled')",
    )

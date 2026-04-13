"""add is_active to business_member

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-04-13 17:00:00.000000

Allows owners to disable/enable team members without removing them.
Existing rows default to TRUE (active) via server_default.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "business_member",
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.create_index(
        "business_member_is_active_idx",
        "business_member",
        ["is_active"],
    )


def downgrade() -> None:
    op.drop_index("business_member_is_active_idx", table_name="business_member")
    op.drop_column("business_member", "is_active")

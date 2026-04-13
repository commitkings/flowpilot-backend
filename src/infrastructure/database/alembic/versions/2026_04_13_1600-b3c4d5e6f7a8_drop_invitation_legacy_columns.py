"""drop legacy invitation columns superseded by rename migration

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-04-13 16:00:00.000000

a2b3c4d5e6f7 added invited_email/invited_by_user_id/token alongside the
original email/invited_by/token_hash columns (because both sets existed).
This migration drops the legacy columns and their constraints so the table
matches the SQLAlchemy model exactly.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(bind, table: str, column: str) -> bool:
    result = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.fetchone() is not None


def _index_exists(bind, index_name: str) -> bool:
    result = bind.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :i"),
        {"i": index_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    bind = op.get_bind()

    # Drop indexes that reference legacy columns first
    if _index_exists(bind, "invitation_email_idx"):
        op.drop_index("invitation_email_idx", table_name="invitation")

    if _index_exists(bind, "invitation_invited_by_idx"):
        op.drop_index("invitation_invited_by_idx", table_name="invitation")

    # Drop the unique constraint on token_hash before dropping the column
    bind.execute(
        sa.text(
            "ALTER TABLE invitation DROP CONSTRAINT IF EXISTS invitation_token_hash_key"
        )
    )

    # Drop legacy columns
    if _column_exists(bind, "invitation", "email"):
        op.drop_column("invitation", "email")

    if _column_exists(bind, "invitation", "invited_by"):
        op.drop_column("invitation", "invited_by")

    if _column_exists(bind, "invitation", "token_hash"):
        op.drop_column("invitation", "token_hash")

    # Drop extra columns not in the model
    if _column_exists(bind, "invitation", "accepted_at"):
        op.drop_column("invitation", "accepted_at")

    if _column_exists(bind, "invitation", "updated_at"):
        # Drop the trigger that writes to updated_at first
        bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_invitation_updated_at ON invitation"))
        op.drop_column("invitation", "updated_at")


def downgrade() -> None:
    # Not reversible — legacy columns are gone
    pass

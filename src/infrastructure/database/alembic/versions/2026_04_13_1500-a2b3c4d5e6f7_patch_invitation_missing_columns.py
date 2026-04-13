"""patch invitation table — rename columns to match SQLAlchemy model

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-04-13 15:00:00.000000

The invitation table was manually created with different column names than
the SQLAlchemy model expects:
  email        -> invited_email
  invited_by   -> invited_by_user_id
  token_hash   -> token
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
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

    # email -> invited_email
    if _column_exists(bind, "invitation", "email") and not _column_exists(bind, "invitation", "invited_email"):
        # Drop old email index before renaming
        if _index_exists(bind, "invitation_email_idx"):
            op.drop_index("invitation_email_idx", table_name="invitation")
        if _index_exists(bind, "invitation_email_status_idx"):
            op.drop_index("invitation_email_status_idx", table_name="invitation")
        op.alter_column("invitation", "email", new_column_name="invited_email")

    # invited_by -> invited_by_user_id
    if _column_exists(bind, "invitation", "invited_by") and not _column_exists(bind, "invitation", "invited_by_user_id"):
        if _index_exists(bind, "invitation_invited_by_idx"):
            op.drop_index("invitation_invited_by_idx", table_name="invitation")
        op.alter_column("invitation", "invited_by", new_column_name="invited_by_user_id")

    # token_hash -> token
    if _column_exists(bind, "invitation", "token_hash") and not _column_exists(bind, "invitation", "token"):
        op.alter_column("invitation", "token_hash", new_column_name="token")

    # Recreate indexes with canonical names
    if not _index_exists(bind, "invitation_email_status_idx"):
        op.create_index("invitation_email_status_idx", "invitation", ["invited_email", "status"])

    if not _index_exists(bind, "invitation_token_idx"):
        op.create_index("invitation_token_idx", "invitation", ["token"])

    if not _index_exists(bind, "invitation_business_id_idx"):
        op.create_index("invitation_business_id_idx", "invitation", ["business_id"])


def downgrade() -> None:
    bind = op.get_bind()

    if _column_exists(bind, "invitation", "invited_email") and not _column_exists(bind, "invitation", "email"):
        op.alter_column("invitation", "invited_email", new_column_name="email")

    if _column_exists(bind, "invitation", "invited_by_user_id") and not _column_exists(bind, "invitation", "invited_by"):
        op.alter_column("invitation", "invited_by_user_id", new_column_name="invited_by")

    if _column_exists(bind, "invitation", "token") and not _column_exists(bind, "invitation", "token_hash"):
        op.alter_column("invitation", "token", new_column_name="token_hash")

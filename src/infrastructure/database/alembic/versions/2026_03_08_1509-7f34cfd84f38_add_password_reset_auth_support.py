"""add password reset auth support

Revision ID: 7f34cfd84f38
Revises: 98276fd36c76
Create Date: 2026-03-08 15:09:38.230454

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "7f34cfd84f38"
down_revision: Union[str, Sequence[str], None] = "98276fd36c76"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "user",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.add_column("user", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.add_column(
        "user",
        sa.Column("password_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_table(
        "password_reset_token",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "password_reset_token_user_id_idx",
        "password_reset_token",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "password_reset_token_expires_at_idx",
        "password_reset_token",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "password_reset_token_expires_at_idx",
        table_name="password_reset_token",
    )
    op.drop_index("password_reset_token_user_id_idx", table_name="password_reset_token")
    op.drop_table("password_reset_token")

    op.drop_column("user", "password_changed_at")
    op.drop_column("user", "password_hash")

    op.execute(
        "UPDATE \"user\" SET external_id = 'system:' || id::text "
        "WHERE external_id IS NULL OR external_id = ''"
    )
    op.alter_column(
        "user",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )

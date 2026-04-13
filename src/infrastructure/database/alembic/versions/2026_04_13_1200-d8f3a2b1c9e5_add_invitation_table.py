"""add invitation table

Revision ID: d8f3a2b1c9e5
Revises: b4e5f6a7c8d9
Create Date: 2026-04-13 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

# revision identifiers, used by Alembic.
revision: str = "d8f3a2b1c9e5"
down_revision: Union[str, None] = "b4e5f6a7c8d9"
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


def upgrade() -> None:
    bind = op.get_bind()

    if not bind.dialect.has_table(bind, "invitation"):
        op.create_table(
            "invitation",
            sa.Column(
                "id",
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "business_id",
                UUID(as_uuid=True),
                sa.ForeignKey("business.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("invited_email", sa.String(255), nullable=False),
            sa.Column(
                "role",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'analyst'"),
            ),
            sa.Column(
                "invited_by_user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("user.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("token", sa.String(128), nullable=False, unique=True),
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.CheckConstraint(
                "role IN ('approver', 'analyst')",
                name="invitation_role_check",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'accepted', 'expired')",
                name="invitation_status_check",
            ),
        )
    else:
        # Table was created manually — backfill any missing columns.
        if not _column_exists(bind, "invitation", "invited_email"):
            op.add_column("invitation", sa.Column("invited_email", sa.String(255), nullable=True))
            bind.execute(sa.text("UPDATE invitation SET invited_email = '' WHERE invited_email IS NULL"))
            op.alter_column("invitation", "invited_email", nullable=False)

        if not _column_exists(bind, "invitation", "role"):
            op.add_column(
                "invitation",
                sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'analyst'")),
            )

        if not _column_exists(bind, "invitation", "invited_by_user_id"):
            op.add_column(
                "invitation",
                sa.Column(
                    "invited_by_user_id",
                    UUID(as_uuid=True),
                    sa.ForeignKey("user.id", ondelete="SET NULL"),
                    nullable=True,
                ),
            )

        if not _column_exists(bind, "invitation", "token"):
            op.add_column("invitation", sa.Column("token", sa.String(128), nullable=True))
            bind.execute(sa.text("UPDATE invitation SET token = gen_random_uuid()::text WHERE token IS NULL"))
            op.alter_column("invitation", "token", nullable=False)

        if not _column_exists(bind, "invitation", "status"):
            op.add_column(
                "invitation",
                sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
            )

        if not _column_exists(bind, "invitation", "expires_at"):
            op.add_column(
                "invitation",
                sa.Column("expires_at", TIMESTAMP(timezone=True), nullable=True),
            )
            bind.execute(sa.text("UPDATE invitation SET expires_at = now() + interval '7 days' WHERE expires_at IS NULL"))
            op.alter_column("invitation", "expires_at", nullable=False)

        if not _column_exists(bind, "invitation", "created_at"):
            op.add_column(
                "invitation",
                sa.Column(
                    "created_at",
                    TIMESTAMP(timezone=True),
                    nullable=False,
                    server_default=sa.text("now()"),
                ),
            )

    op.create_index("invitation_token_idx", "invitation", ["token"], if_not_exists=True)
    op.create_index(
        "invitation_email_status_idx", "invitation", ["invited_email", "status"], if_not_exists=True
    )
    op.create_index("invitation_business_id_idx", "invitation", ["business_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("invitation_business_id_idx", table_name="invitation")
    op.drop_index("invitation_email_status_idx", table_name="invitation")
    op.drop_index("invitation_token_idx", table_name="invitation")
    op.drop_table("invitation")

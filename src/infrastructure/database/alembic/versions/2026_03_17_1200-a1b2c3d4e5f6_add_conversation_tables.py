"""add conversation and conversation_message tables for Phase 2 intent agent

Revision ID: a1b2c3d4e5f6
Revises: 6dcf0bf4dc3c
Create Date: 2026-03-17 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "6dcf0bf4dc3c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'gathering'"), nullable=False
        ),
        sa.Column("current_intent", sa.String(length=64), nullable=True),
        sa.Column(
            "extracted_slots",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "resolved_run_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "message_count",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('gathering', 'confirming', 'executing', 'completed', 'abandoned')",
            name="conversation_status_check",
        ),
        sa.CheckConstraint(
            "current_intent IS NULL OR current_intent IN ("
            "'create_payout_run', 'check_run_status', 'explain_system', "
            "'modify_config', 'greeting', 'farewell', 'unclear')",
            name="conversation_intent_check",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["business.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("conversation_business_id_idx", "conversation", ["business_id"])
    op.create_index("conversation_user_id_idx", "conversation", ["user_id"])
    op.create_index("conversation_status_idx", "conversation", ["status"])
    op.create_index("conversation_run_id_idx", "conversation", ["run_id"])
    op.create_index(
        "conversation_user_updated_idx",
        "conversation",
        ["user_id", sa.text("updated_at DESC")],
    )

    op.create_table(
        "conversation_message",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent_classification", sa.String(length=64), nullable=True),
        sa.Column(
            "extracted_slots", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column(
            "token_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="conversation_message_role_check",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversation.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "conversation_message_conversation_id_idx",
        "conversation_message",
        ["conversation_id"],
    )
    op.create_index(
        "conversation_message_created_at_idx",
        "conversation_message",
        ["created_at"],
        postgresql_using="brin",
    )


def downgrade() -> None:
    op.drop_index(
        "conversation_message_created_at_idx", table_name="conversation_message"
    )
    op.drop_index(
        "conversation_message_conversation_id_idx", table_name="conversation_message"
    )
    op.drop_table("conversation_message")
    op.drop_index("conversation_user_updated_idx", table_name="conversation")
    op.drop_index("conversation_run_id_idx", table_name="conversation")
    op.drop_index("conversation_status_idx", table_name="conversation")
    op.drop_index("conversation_user_id_idx", table_name="conversation")
    op.drop_index("conversation_business_id_idx", table_name="conversation")
    op.drop_table("conversation")

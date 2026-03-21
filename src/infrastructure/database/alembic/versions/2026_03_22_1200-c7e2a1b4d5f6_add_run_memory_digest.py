"""Add run_memory_digest for long-term textual recall (pg_trgm).

Revision ID: c7e2a1b4d5f6
Revises: 8558fff26e1d
Create Date: 2026-03-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c7e2a1b4d5f6"
down_revision: Union[str, Sequence[str], None] = "8558fff26e1d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "run_memory_digest",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("digest_summary", sa.Text(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("blocked_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["business_id"], ["business.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="run_memory_digest_run_id_uq"),
    )
    op.create_index("run_memory_digest_business_id_idx", "run_memory_digest", ["business_id"])
    op.execute(
        "CREATE INDEX run_memory_digest_objective_trgm_idx ON run_memory_digest "
        "USING gin (objective gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX run_memory_digest_digest_trgm_idx ON run_memory_digest "
        "USING gin (digest_summary gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS run_memory_digest_digest_trgm_idx")
    op.execute("DROP INDEX IF EXISTS run_memory_digest_objective_trgm_idx")
    op.drop_index("run_memory_digest_business_id_idx", table_name="run_memory_digest")
    op.drop_table("run_memory_digest")

"""add webhook_deliveries table

Revision ID: aa0415200001
Revises: aa0414200010
Create Date: 2026-04-15 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa0415200001"
down_revision: str | None = "aa0414200010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("webhook_id", sa.UUID(as_uuid=True), sa.ForeignKey("webhook.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_id", sa.UUID(as_uuid=True), sa.ForeignKey("business.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_name", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text, nullable=True),
        sa.Column("status_code", sa.Integer, nullable=True),
        sa.Column("success", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("webhook_deliveries_webhook_id_idx", "webhook_deliveries", ["webhook_id"])
    op.create_index("webhook_deliveries_business_id_idx", "webhook_deliveries", ["business_id"])
    op.create_index("webhook_deliveries_delivered_at_idx", "webhook_deliveries", ["delivered_at"])


def downgrade() -> None:
    op.drop_table("webhook_deliveries")

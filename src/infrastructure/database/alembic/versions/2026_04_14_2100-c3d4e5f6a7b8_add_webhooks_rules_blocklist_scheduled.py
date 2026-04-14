"""add webhook, approval_rule, blocklist_entry, scheduled_run tables

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-14 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── webhook ──────────────────────────────────────────────────────────────
    op.create_table(
        "webhook",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", UUID(as_uuid=True), sa.ForeignKey("business.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("events", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("secret_hash", sa.Text, nullable=False),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_triggered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("webhook_business_id_idx", "webhook", ["business_id"])

    # ── approval_rule ─────────────────────────────────────────────────────────
    op.create_table(
        "approval_rule",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", UUID(as_uuid=True), sa.ForeignKey("business.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("condition", sa.Text, nullable=False),
        sa.Column("threshold", sa.Numeric(18, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("required_approvers", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("approver_roles", JSONB, nullable=False, server_default=sa.text("'[\"approver\"]'::jsonb")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "condition IN ('amount_above', 'risk_score_above', 'always')",
            name="approval_rule_condition_check",
        ),
        sa.CheckConstraint("required_approvers >= 1", name="approval_rule_min_approvers_check"),
    )
    op.create_index("approval_rule_business_id_idx", "approval_rule", ["business_id"])

    # ── blocklist_entry ───────────────────────────────────────────────────────
    op.create_table(
        "blocklist_entry",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", UUID(as_uuid=True), sa.ForeignKey("business.id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_by", UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "type IN ('account_number', 'beneficiary_name', 'bank_code')",
            name="blocklist_entry_type_check",
        ),
    )
    op.create_index("blocklist_entry_business_id_idx", "blocklist_entry", ["business_id"])
    op.create_index("blocklist_entry_type_value_idx", "blocklist_entry", ["business_id", "type", "value"])

    # ── scheduled_run ─────────────────────────────────────────────────────────
    op.create_table(
        "scheduled_run",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", UUID(as_uuid=True), sa.ForeignKey("business.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("objective", sa.Text, nullable=False),
        sa.Column("cron_expression", sa.String(128), nullable=False),
        sa.Column("frequency_label", sa.String(64), nullable=False),
        sa.Column("next_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("scheduled_run_business_id_idx", "scheduled_run", ["business_id"])
    op.create_index("scheduled_run_next_run_at_idx", "scheduled_run", ["next_run_at"])


def downgrade() -> None:
    op.drop_index("scheduled_run_next_run_at_idx", table_name="scheduled_run")
    op.drop_index("scheduled_run_business_id_idx", table_name="scheduled_run")
    op.drop_table("scheduled_run")

    op.drop_index("blocklist_entry_type_value_idx", table_name="blocklist_entry")
    op.drop_index("blocklist_entry_business_id_idx", table_name="blocklist_entry")
    op.drop_table("blocklist_entry")

    op.drop_index("approval_rule_business_id_idx", table_name="approval_rule")
    op.drop_table("approval_rule")

    op.drop_index("webhook_business_id_idx", table_name="webhook")
    op.drop_table("webhook")

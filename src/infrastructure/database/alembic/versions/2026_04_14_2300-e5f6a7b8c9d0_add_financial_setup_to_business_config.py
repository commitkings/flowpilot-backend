"""add financial setup columns to business_config

Revision ID: aa0414200005
Revises: aa0414200004
Create Date: 2026-04-14 23:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "aa0414200005"
down_revision = "aa0414200004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merchant registration state (e.g. "Lagos")
    op.add_column(
        "business_config",
        sa.Column("merchant_state", sa.String(100), nullable=True),
    )
    # Daily cap on total payout volume (₦)
    op.add_column(
        "business_config",
        sa.Column("daily_payout_limit", sa.Numeric(18, 2), nullable=True),
    )
    # Per-transaction cap (₦)
    op.add_column(
        "business_config",
        sa.Column("single_payout_cap", sa.Numeric(18, 2), nullable=True),
    )
    # Risk score threshold above which a transaction is flagged (0.00–1.00)
    op.add_column(
        "business_config",
        sa.Column("risk_alert_threshold", sa.Numeric(5, 4), nullable=True),
    )
    # Percentage of liquidity buffer below which an alert fires (0–100)
    op.add_column(
        "business_config",
        sa.Column("liquidity_alert_buffer", sa.Numeric(5, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("business_config", "liquidity_alert_buffer")
    op.drop_column("business_config", "risk_alert_threshold")
    op.drop_column("business_config", "single_payout_cap")
    op.drop_column("business_config", "daily_payout_limit")
    op.drop_column("business_config", "merchant_state")

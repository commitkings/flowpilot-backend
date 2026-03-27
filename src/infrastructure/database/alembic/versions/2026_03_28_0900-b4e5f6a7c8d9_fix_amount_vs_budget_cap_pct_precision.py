"""fix amount_vs_budget_cap_pct precision

Revision ID: b4e5f6a7c8d9
Revises: f3c1d9a7b2e4
Create Date: 2026-03-28 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e5f6a7c8d9'
down_revision: Union[str, None] = 'f3c1d9a7b2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Increase precision of amount_vs_budget_cap_pct to allow values > 1000.
    
    Previous: NUMERIC(7,4) allowed max 999.9999
    New: NUMERIC(10,4) allows max 999999.9999
    
    This is needed because the percentage can exceed 1000% when payout amounts
    are much larger than budget caps.
    """
    op.alter_column(
        'risk_score_feature',
        'amount_vs_budget_cap_pct',
        existing_type=sa.Numeric(precision=7, scale=4),
        type_=sa.Numeric(precision=10, scale=4),
        existing_nullable=True
    )


def downgrade() -> None:
    """Revert to original precision (may truncate large values)."""
    op.alter_column(
        'risk_score_feature',
        'amount_vs_budget_cap_pct',
        existing_type=sa.Numeric(precision=10, scale=4),
        type_=sa.Numeric(precision=7, scale=4),
        existing_nullable=True
    )

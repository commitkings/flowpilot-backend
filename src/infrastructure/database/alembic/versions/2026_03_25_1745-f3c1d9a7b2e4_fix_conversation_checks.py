"""fix conversation status and intent constraints

Revision ID: f3c1d9a7b2e4
Revises: 8558fff26e1d
Create Date: 2026-03-25 17:45:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3c1d9a7b2e4"
down_revision: Union[str, Sequence[str], None] = "c7e2a1b4d5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("conversation_status_check", "conversation", type_="check")
    op.create_check_constraint(
        "conversation_status_check",
        "conversation",
        "status IN ('gathering', 'confirming', 'awaiting_approval', 'executing', 'completed', 'abandoned')",
    )

    op.drop_constraint("conversation_intent_check", "conversation", type_="check")
    op.create_check_constraint(
        "conversation_intent_check",
        "conversation",
        "current_intent IS NULL OR current_intent IN ("
        "'create_payout_run', 'check_run_status', 'review_candidates', "
        "'approve_reject', 'explain_system', 'view_audit', "
        "'modify_config', 'greeting', 'farewell', 'acknowledgement', "
        "'unclear')",
    )


def downgrade() -> None:
    op.drop_constraint("conversation_intent_check", "conversation", type_="check")
    op.create_check_constraint(
        "conversation_intent_check",
        "conversation",
        "current_intent IS NULL OR current_intent IN ("
        "'create_payout_run', 'check_run_status', 'explain_system', "
        "'modify_config', 'greeting', 'farewell', 'unclear')",
    )

    op.drop_constraint("conversation_status_check", "conversation", type_="check")
    op.create_check_constraint(
        "conversation_status_check",
        "conversation",
        "status IN ('gathering', 'confirming', 'executing', 'completed', 'abandoned')",
    )

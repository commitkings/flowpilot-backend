"""baseline_8_tables_20_indexes

Revision ID: 0001
Revises: 
Create Date: 2026-02-22 15:41:59.334496

"""
from pathlib import Path
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _split_sql_statements(script: str) -> list[str]:
    """Split SQL script into executable statements while preserving $$ bodies."""
    statements: list[str] = []
    buffer: list[str] = []

    in_single_quote = False
    in_double_quote = False
    in_line_comment = False
    in_block_comment = False
    dollar_quote_tag: str | None = None

    index = 0
    length = len(script)

    while index < length:
        char = script[index]
        next_char = script[index + 1] if index + 1 < length else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            index += 1
            continue

        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
            continue

        if dollar_quote_tag:
            if script.startswith(dollar_quote_tag, index):
                buffer.append(dollar_quote_tag)
                index += len(dollar_quote_tag)
                dollar_quote_tag = None
                continue
            buffer.append(char)
            index += 1
            continue

        if not in_single_quote and not in_double_quote:
            if char == "-" and next_char == "-":
                in_line_comment = True
                index += 2
                continue
            if char == "/" and next_char == "*":
                in_block_comment = True
                index += 2
                continue

            dollar_match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", script[index:])
            if dollar_match:
                dollar_quote_tag = dollar_match.group(0)
                buffer.append(dollar_quote_tag)
                index += len(dollar_quote_tag)
                continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            buffer.append(char)
            index += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            buffer.append(char)
            index += 1
            continue

        if char == ";" and not in_single_quote and not in_double_quote:
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    trailing_statement = "".join(buffer).strip()
    if trailing_statement:
        statements.append(trailing_statement)

    return statements


def upgrade() -> None:
    """Upgrade schema."""
    sql_file_path = Path(__file__).resolve().parents[2] / "migrations" / "001_initial_schema.sql"
    script_content = sql_file_path.read_text(encoding="utf-8")

    # Execute only the UP migration section from the bootstrap SQL.
    up_section = script_content.split("-- DOWN MIGRATION", maxsplit=1)[0]
    for statement in _split_sql_statements(up_section):
        normalized_statement = statement.strip()
        if normalized_statement.upper() in {"BEGIN", "COMMIT"}:
            continue
        op.execute(sa.text(normalized_statement))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("DROP TABLE IF EXISTS audit_log"))
    op.execute(sa.text("DROP TABLE IF EXISTS payout_candidate"))
    op.execute(sa.text("DROP TABLE IF EXISTS payout_batch"))
    op.execute(sa.text("DROP TABLE IF EXISTS transaction"))
    op.execute(sa.text("DROP TABLE IF EXISTS plan_step"))
    op.execute(sa.text("DROP TABLE IF EXISTS agent_run"))
    op.execute(sa.text("DROP TABLE IF EXISTS institution"))
    op.execute(sa.text("DROP TABLE IF EXISTS operator"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS set_updated_at()"))

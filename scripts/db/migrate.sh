#!/usr/bin/env bash
# FlowPilot Database Migration Script
# Usage:
#   ./scripts/db/migrate.sh                   — apply all pending migrations
#   ./scripts/db/migrate.sh status            — show current revision
#   ./scripts/db/migrate.sh new "description" — generate new migration from ORM diff
#   ./scripts/db/migrate.sh down [n]          — roll back n revisions (default 1)
#   ./scripts/db/migrate.sh history           — show migration history
#   ./scripts/db/migrate.sh heads             — show pending migrations
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Resolve DATABASE_URL with sensible local default
export DATABASE_URL="${DATABASE_URL:-postgresql://m1pro@localhost/flowpilot}"

CMD="${1:-upgrade}"
shift 2>/dev/null || true

case "$CMD" in
  upgrade|up|"")
    echo "▸ Applying pending migrations…"
    python -m alembic upgrade head
    echo "✓ Database is at head."
    ;;
  status|current)
    python -m alembic current
    ;;
  new|generate|auto)
    MSG="${1:?Usage: migrate.sh new \"description\"}"
    echo "▸ Generating migration: $MSG"
    python -m alembic revision --autogenerate -m "$MSG"
    echo "✓ Review the generated file before committing."
    ;;
  down|downgrade)
    STEPS="${1:-1}"
    echo "▸ Rolling back $STEPS revision(s)…"
    python -m alembic downgrade "-$STEPS"
    echo "✓ Downgrade complete."
    ;;
  history)
    python -m alembic history --verbose
    ;;
  heads)
    python -m alembic heads
    ;;
  check)
    echo "▸ Checking for unapplied model changes…"
    python -m alembic check 2>&1 && echo "✓ No pending changes." || echo "⚠ Model changes detected — run: ./scripts/db/migrate.sh new \"description\""
    ;;
  *)
    echo "Unknown command: $CMD"
    echo "Usage: migrate.sh [upgrade|status|new|down|history|heads|check]"
    exit 1
    ;;
esac

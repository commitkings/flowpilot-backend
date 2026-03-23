#!/usr/bin/env bash
set -euo pipefail

DB_NAME="${FLOWPILOT_DB_NAME:-flowpilot}"
DB_USER="${FLOWPILOT_DB_USER:-m1pro}"
MIGRATION_FILE="$(dirname "$0")/../../src/infrastructure/database/migrations/001_initial_schema.sql"

echo "=== FlowPilot Database Setup ==="

if ! command -v psql &> /dev/null; then
    echo "ERROR: psql not found. Install PostgreSQL first."
    exit 1
fi

if ! pg_isready -q 2>/dev/null; then
    echo "ERROR: PostgreSQL is not running."
    exit 1
fi

if psql -U "$DB_USER" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1; then
    echo "Database '$DB_NAME' already exists."
    read -rp "Drop and recreate? (y/N): " confirm
    if [[ "$confirm" == [yY] ]]; then
        psql -U "$DB_USER" -d postgres -c "DROP DATABASE $DB_NAME;"
        echo "Dropped '$DB_NAME'."
    else
        echo "Applying migration to existing database..."
        psql -U "$DB_USER" -d "$DB_NAME" -f "$MIGRATION_FILE"
        echo "Done."
        exit 0
    fi
fi

echo "Creating database '$DB_NAME'..."
psql -U "$DB_USER" -d postgres -c "CREATE DATABASE $DB_NAME;"

echo "Applying migration..."
psql -U "$DB_USER" -d "$DB_NAME" -f "$MIGRATION_FILE"

echo ""
echo "=== Validation ==="
psql -U "$DB_USER" -d "$DB_NAME" -c "
SELECT 'Tables' AS check_type, COUNT(*)::TEXT AS result
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
UNION ALL
SELECT 'Indexes', COUNT(*)::TEXT
FROM pg_indexes WHERE schemaname = 'public'
UNION ALL
SELECT 'FK Constraints', COUNT(*)::TEXT
FROM information_schema.table_constraints
WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'public'
UNION ALL
SELECT 'CHECK Constraints', COUNT(*)::TEXT
FROM information_schema.table_constraints
WHERE constraint_type = 'CHECK' AND table_schema = 'public'
UNION ALL
SELECT 'Triggers', COUNT(*)::TEXT
FROM information_schema.triggers WHERE trigger_schema = 'public';
"

echo ""
echo "✅ FlowPilot database '$DB_NAME' is ready."

-- Migration: 002_seed_default_operator.sql
-- Description: Seed a default system operator for development/testing
-- Author: FlowPilot Team
-- Database: PostgreSQL 17+

BEGIN;

INSERT INTO operator (id, display_name, email, role, is_active)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'System Operator',
    'system@flowpilot.local',
    'admin',
    TRUE
)
ON CONFLICT (id) DO NOTHING;

COMMIT;

-- Migration: 003_add_assigned_approver.sql
-- Description: Add assigned_to_id to agent_run for approval assignment tracking
-- Author: FlowPilot Team

BEGIN;

ALTER TABLE agent_run
    ADD COLUMN IF NOT EXISTS assigned_to_id UUID
        REFERENCES "user"(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS agent_run_assigned_to_id_idx
    ON agent_run (assigned_to_id)
    WHERE assigned_to_id IS NOT NULL;

COMMIT;

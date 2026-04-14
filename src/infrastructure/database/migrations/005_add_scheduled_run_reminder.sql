-- Migration 005: Add last_reminded_at to scheduled_run
-- Tracks which next_run_at occurrence we already sent the day-before reminder
-- for, preventing duplicate notifications if the scheduler loop runs multiple
-- times within the same 25-hour window.

ALTER TABLE scheduled_run
    ADD COLUMN IF NOT EXISTS last_reminded_at TIMESTAMPTZ;

COMMENT ON COLUMN scheduled_run.last_reminded_at IS
    'Stores the next_run_at value for which the day-before reminder was last sent. '
    'If last_reminded_at = next_run_at the reminder has already been sent for that occurrence.';

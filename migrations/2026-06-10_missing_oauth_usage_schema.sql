-- Fix: Add missing created_at column to oauth_usage_logs
-- The audit route at src/admin/cost_model.py queries created_at but the table only had called_at
-- This migration adds created_at as a copy of called_at to maintain backward compatibility

BEGIN;

ALTER TABLE oauth_usage_logs ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

-- Populate created_at from called_at for existing rows
UPDATE oauth_usage_logs
SET created_at = called_at
WHERE created_at IS NULL AND called_at IS NOT NULL;

-- Make created_at match called_at for consistency
-- Use a trigger to keep them in sync on future inserts
CREATE OR REPLACE FUNCTION sync_oauth_usage_logs_created_at()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.created_at IS NULL THEN
    NEW.created_at := NEW.called_at;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS oauth_usage_logs_created_at_trigger ON oauth_usage_logs;

CREATE TRIGGER oauth_usage_logs_created_at_trigger
BEFORE INSERT ON oauth_usage_logs
FOR EACH ROW
EXECUTE FUNCTION sync_oauth_usage_logs_created_at();

COMMIT;

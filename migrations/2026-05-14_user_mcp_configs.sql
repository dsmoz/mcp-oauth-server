-- Add config_schema to mcp_catalogue so admins can declare per-MCP credential fields.
-- Format: JSON array of field descriptors, e.g.:
--   [{"key":"api_key","label":"API Key","type":"password","required":true,"placeholder":"Enter your key"}]
ALTER TABLE mcp_catalogue
  ADD COLUMN IF NOT EXISTS config_schema JSONB DEFAULT NULL;

-- Per-user config values for MCPs that require credentials.
-- Rows survive MCP unpublish so re-enabling restores config.
CREATE TABLE IF NOT EXISTS user_mcp_configs (
  user_id   TEXT        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  mcp_slug  TEXT        NOT NULL,
  config    JSONB       NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, mcp_slug)
);

-- Allow service role full access; block direct user reads (RLS enforced at app layer).
ALTER TABLE user_mcp_configs ENABLE ROW LEVEL SECURITY;

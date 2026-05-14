-- Per-user MCP credentials storage
-- Stores credentials that individual users provide when activating MCPs
-- requiring external API keys (e.g. KoboToolbox, Zoom personal tokens).
-- Gateway reads these at call time and forwards as X-MCP-Credentials header.

CREATE TABLE IF NOT EXISTS client_mcp_credentials (
    user_id     text        NOT NULL,
    mcp_slug    text        NOT NULL,
    credentials jsonb       NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, mcp_slug)
);

-- Schema that defines what credentials each MCP needs.
-- Empty object means no credentials required (most MCPs).
-- Example: {"kobo_api_key": {"type": "string", "label": "API Key", "required": true}}
ALTER TABLE mcp_catalogue
    ADD COLUMN IF NOT EXISTS credentials_schema jsonb NOT NULL DEFAULT '{}'::jsonb;

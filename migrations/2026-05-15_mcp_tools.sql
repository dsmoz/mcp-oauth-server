-- Tool registry: one row per tool per upstream MCP.
-- Populated by admin sync; used for cross-MCP tool search and tool counts.
CREATE TABLE IF NOT EXISTS mcp_tools (
    mcp_slug     text        NOT NULL REFERENCES mcp_catalogue(slug) ON DELETE CASCADE,
    tool_name    text        NOT NULL,
    description  text,
    input_schema jsonb,
    synced_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (mcp_slug, tool_name)
);

CREATE INDEX IF NOT EXISTS mcp_tools_slug_idx ON mcp_tools (mcp_slug);

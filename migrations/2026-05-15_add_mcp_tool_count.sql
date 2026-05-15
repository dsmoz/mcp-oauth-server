ALTER TABLE mcp_catalogue ADD COLUMN IF NOT EXISTS tool_count INT;
COMMENT ON COLUMN mcp_catalogue.tool_count IS 'Cached count of tools exposed by upstream MCP. Refreshed on description regenerate/publish.';

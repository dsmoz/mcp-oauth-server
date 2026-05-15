-- Split mcp_catalogue.description into human-facing + agent-facing fields.
-- `description` remains the human-facing label shown in the portal.
-- `description_agent` is the longer, technical text fed to LLMs (auto-generated from tool list).
ALTER TABLE mcp_catalogue ADD COLUMN IF NOT EXISTS description_agent TEXT;
UPDATE mcp_catalogue SET description_agent = description WHERE description_agent IS NULL;
COMMENT ON COLUMN mcp_catalogue.description IS 'Human-facing description shown in portal toolbox/catalog.';
COMMENT ON COLUMN mcp_catalogue.description_agent IS 'Agent-facing description (technical, includes tool list) fed to LLMs via browse_mcps/list_mcps.';

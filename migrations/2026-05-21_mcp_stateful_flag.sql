-- Flag MCPs whose tools mutate cross-call state held in upstream memory.
-- Stateless `invoke_mcp_tool` drops that state between calls (each request lands
-- in a fresh worker context), so stateful MCPs must be driven via their
-- `run(code)` tool which executes the full script in one upstream call.

ALTER TABLE mcp_catalogue
  ADD COLUMN IF NOT EXISTS is_stateful BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN mcp_catalogue.is_stateful IS
  'When TRUE, invoke_mcp_tool refuses non-discovery tool calls and directs the agent to use run(code). Set for code-execution MCPs that mutate in-process state (e.g. mcp-deck).';

UPDATE mcp_catalogue SET is_stateful = TRUE WHERE slug = 'mcp-deck';

-- Set credit_cost_per_call for all MCP catalogue entries
UPDATE mcp_catalogue SET credit_cost_per_call = 1 WHERE slug = 'kobotoolbox';
UPDATE mcp_catalogue SET credit_cost_per_call = 1 WHERE slug = 'microsoft365';
UPDATE mcp_catalogue SET credit_cost_per_call = 1 WHERE slug = 'asset-manager';
UPDATE mcp_catalogue SET credit_cost_per_call = 2 WHERE slug = 'academia';
UPDATE mcp_catalogue SET credit_cost_per_call = 2 WHERE slug = 'surveylab';
UPDATE mcp_catalogue SET credit_cost_per_call = 3 WHERE slug = 'linguist';
UPDATE mcp_catalogue SET credit_cost_per_call = 5 WHERE slug = 'loom';
UPDATE mcp_catalogue SET credit_cost_per_call = 5 WHERE slug = 'dsmoz-nexus';
UPDATE mcp_catalogue SET credit_cost_per_call = 5 WHERE slug = 'design-engine';
UPDATE mcp_catalogue SET credit_cost_per_call = 3 WHERE slug = 'scholar';

-- Top-up request queue (no payment gateway — admin approves manually via Telegram)
CREATE TABLE IF NOT EXISTS credit_topup_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES users(user_id),
    amount float NOT NULL,
    note text DEFAULT '',
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz DEFAULT now(),
    reviewed_at timestamptz,
    reviewed_by text
);

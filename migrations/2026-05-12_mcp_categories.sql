-- Categories for MCP catalogue entries.
-- Managed via /admin/categories. Referenced by mcp_catalogue.category by name (no FK,
-- so deleting a category does not cascade — existing entries keep the string value).

CREATE TABLE IF NOT EXISTS mcp_categories (
    name TEXT PRIMARY KEY,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mcp_categories_sort_order_idx
    ON mcp_categories (sort_order, name);

-- Seed from existing distinct category values in the catalogue
INSERT INTO mcp_categories (name, sort_order)
SELECT DISTINCT category, 0
FROM mcp_catalogue
WHERE category IS NOT NULL AND category <> ''
ON CONFLICT (name) DO NOTHING;

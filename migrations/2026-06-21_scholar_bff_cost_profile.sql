-- 2026-06-21: register mcp-scholar-bff internal slug + cost profile
-- Used by /api/v1/wallet/debit so scholar BFF usage flows through the
-- gateway's existing compute_cost() pipeline and oauth_usage_logs.

begin;

insert into public.mcp_catalogue (slug, name, description, category, upstream_url, is_published, tier)
values (
  'mcp-scholar-bff',
  'Scholar BFF (internal)',
  'Internal pseudo-MCP slug used to bill scholar BFF usage via /api/v1/wallet/debit. Not user-facing.',
  'internal',
  'internal://scholar-bff',
  false,
  'standard'
)
on conflict (slug) do nothing;

insert into public.mcp_cost_profile (mcp_slug, compute_rate_name, fixed_surcharge_usd)
values ('mcp-scholar-bff', 'default', 0.0)
on conflict (mcp_slug) do nothing;

commit;

# Per-User MCP Credentials — Plan

Status: in progress (DB + portal UI shipped in PR #43; gateway + per-MCP adapters pending).

## Problem

Some upstream MCPs (mcp-scholar, mcp-kobotoolbox, mcp-docuseal) need credentials that belong to the *end user*, not to the platform — e.g. a user's personal Zotero API key, KoboToolbox token, or DocuSeal account key. Today every MCP runs with one global env-var key per service, so all users effectively share whichever account the operator configured.

## Decision

**Gateway header injection.** Per-user credentials are stored in `user_mcp_configs`, the gateway reads them on each call, and forwards them to the upstream MCP as `X-MCP-{KEY}` request headers. Upstream MCPs read those headers per request and fall back to env vars when the headers are absent (preserves stdio/local mode).

Rejected alternatives:
- **MCP reads DB itself** — secret sprawl (every MCP needs Supabase service key), per-call DB latency, schema coupling.
- **Per-request `KoboClient` refactor without header transport** — strict superset of header injection, no upside.

Trust boundary: gateway → upstream traffic is on Railway's private network. Plaintext on the wire is acceptable for now. If we ever host an MCP off-Railway, add HMAC signature or mTLS on the gateway-to-MCP hop.

## Components

### Database — DONE (PR #43)
- `mcp_catalogue.config_schema JSONB` — admin-declared array of credential field descriptors
- `user_mcp_configs (user_id, mcp_slug, config JSONB, updated_at)` with FK cascade + RLS

### Portal — DONE (PR #43)
- Gear icon on `/portal/mcps` rows that have `config_schema`, opens collapsible panel
- `POST /portal/mcps/{slug}/config` — upserts user config

### Gateway — TODO

`src/gateway/routes.py`:
- `_load_user_mcp_config(user_id, slug) -> dict` — reads `user_mcp_configs.config`, 60s TTL cache keyed `(user_id, slug)`. Invalidate on `add_mcp` / `remove_mcp` / portal config save.
- `_user_config_headers(config_schema, user_config) -> dict[str,str]` — for each field in schema, emits `X-MCP-{KEY}: value` (key uppercased, dashes preserved). Skips empty values; preserves order; never emits if `config_schema` is null.
- Patch all four upstream call sites to pass `extra_headers=`:
  - `invoke_mcp_tool` handler (line ~903)
  - `search_tools` fan-out (line ~706)
  - `list_mcp_tools` discovery (line ~664)
  - `read_resource` (line ~1026)
- `rest_proxy.py` — same logic for `/api/plugin/*` (only mcp-scholar today).

`src/gateway/upstream.py`:
- `call_upstream_tool(...)` and `call_upstream_tool_structured(...)` gain `extra_headers: dict | None = None` kwarg, merged into the headers dict before the SSE/streamable client call.
- Same for `_list_tools_via_url`, `_read_resource_via_url`.
- Cache invalidation: portal `POST /portal/mcps/{slug}/config` calls `gateway.routes._invalidate_user_tool_cache(user_id)` and the new config cache.

### Per-MCP adapter pattern — TODO

For each MCP that needs user credentials:

1. **ASGI middleware** (sits next to `BearerAuthMiddleware`) — reads any header matching `X-MCP-*` from `scope["headers"]`, stashes the dict in a `contextvars.ContextVar` for the duration of the request.
2. **Config helper** — replace `os.getenv("KOBO_API_KEY")` with `get_user_credential("kobo_api_key")`, which:
   - Reads from the contextvar first (per-request, per-tenant)
   - Falls back to `os.getenv("KOBO_API_KEY")` (stdio / local dev)
   - Falls back to `None` → MCP returns a clean "credentials not configured" error
3. **Drop `@lru_cache`** on any config singleton that holds credentials. Process-level singletons leak across tenants.
4. **Connection objects** (e.g. `httpx.Client`, `KoboClient`) must build per-request — or rebuild headers per request — never cache headers at construction time.

### Catalogue seed — TODO

```sql
UPDATE mcp_catalogue SET config_schema = '[
  {"key":"zotero_api_key","label":"Zotero API Key","type":"password","required":true,"placeholder":"Get from zotero.org/settings/keys"},
  {"key":"zotero_user_id","label":"Zotero User ID","type":"text","required":true,"placeholder":"Numeric user ID"},
  {"key":"zotero_library_type","label":"Library Type","type":"text","required":false,"placeholder":"user (default) or group"}
]'::jsonb WHERE slug = 'mcp-scholar';

UPDATE mcp_catalogue SET config_schema = '[
  {"key":"kobo_api_key","label":"KoboToolbox API Token","type":"password","required":true,"placeholder":"Account Settings → Security → API Key"},
  {"key":"kobo_server_url","label":"KoboToolbox Server","type":"url","required":false,"placeholder":"https://kf.kobotoolbox.org (or eu.kobotoolbox.org)"}
]'::jsonb WHERE slug = 'mcp-kobotoolbox';

UPDATE mcp_catalogue SET config_schema = '[
  {"key":"docuseal_api_key","label":"DocuSeal API Key","type":"password","required":true,"placeholder":"app.docuseal.com/settings/api"},
  {"key":"docuseal_region","label":"Region","type":"text","required":false,"placeholder":"US (default) or EU"}
]'::jsonb WHERE slug = 'mcp-docuseal';
```

## Header naming convention

- Field key: lowercase snake_case (`zotero_api_key`)
- Header name: `X-MCP-` + key uppercased + underscores → dashes (`X-MCP-ZOTERO-API-KEY`)
- Multiple fields = multiple headers, no JSON blob (lets MCPs route to the right config slot without parsing)

## Phases

1. **Gateway plumbing** — load config, build headers, plumb through `extra_headers` in 4 call sites + rest_proxy. Self-contained, no upstream changes needed yet.
2. **mcp-scholar adapter** — pilot. ZoteroConnection reads from contextvar.
3. **mcp-kobotoolbox adapter** — repeat pattern.
4. **mcp-docuseal adapter** — repeat pattern.
5. **Seed `config_schema`** for the three MCPs above (after their adapters deploy — a missing adapter just means the headers are ignored, no breakage).
6. **E2E test**: two users in portal, different Zotero keys, confirm scholar isolates correctly.

## Out of scope (for now)

- mcp-zoom (S2S OAuth, org-level)
- mcp-microsoft365 (delegated user tokens, already per-user via token cache file — different model)
- mcp-brevo / mcp-socials / mcp-surveylab (internal org-shared keys are correct)
- Encryption at rest in `user_mcp_configs.config` (RLS service-role-only is sufficient until off-Railway hosting)
- HMAC/mTLS on gateway→MCP hop (defer until off-Railway)

## Failure modes & responses

| Scenario | Response |
|---|---|
| User enabled MCP but didn't fill required config | Upstream returns `{"error":"credentials not configured","field":"zotero_api_key"}`. Gateway surfaces as-is. |
| User key is invalid (401 from external service) | Upstream raises 401. Gateway already maps to actionable error message. |
| Admin sets bad `config_schema` JSON | Admin form rejects on save (already implemented). |
| MCP without adapter receives `X-MCP-*` headers | Headers ignored, env-var path used. Safe. |
| Adapter without catalogue `config_schema` | Headers absent, env-var fallback. Safe. |

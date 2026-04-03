# Gateway & Client Portal — Design Spec

**Date:** 2026-04-03  
**Status:** Approved  
**Roadmap item:** v1.4 — DS-MOZ Intelligence Gateway + Client Self-Service Portal

---

## Context

Registered clients currently receive a `client_id` and `client_secret` and must configure each MCP server individually. This migration introduces:

1. **A single gateway MCP endpoint** per client — `GET /gateway/{client_id}` — that the LLM connects to once and gets access to all the client's enabled tools via progressive search disclosure.
2. **A client self-service portal** — `/portal/` — where clients log in with their credentials and toggle which MCPs they want access to, then download their config.
3. **An admin MCP catalogue** — `/admin/catalogue` — where admins publish MCP servers (name, description, category, upstream SSE URL).

---

## Decisions

| Topic | Decision |
|---|---|
| Gateway transport | SSE (matches upstream MCP servers) |
| Tool disclosure | 4 meta-tools only: `search_tools`, `list_mcps`, `list_tools`, `call_tool` |
| Upstream protocol | SSE — all deployed MCP servers expose `/sse` |
| Portal auth | `client_id` + `client_secret` → signed session cookie (`itsdangerous`) |
| MCP selection storage | Reuse `allowed_mcp_resources: list[str]` on `oauth_clients` — stores list of catalogue `slug` values |
| New DB table | `mcp_catalogue` — admin-managed, one row per published MCP |
| Portal layout | Sidebar: Overview / My MCPs / Setup Guide |
| Config output | Copy-paste `claude_desktop_config.json` block + ChatGPT OAuth block + download button |

---

## Database

### New table: `mcp_catalogue`

```sql
CREATE TABLE public.mcp_catalogue (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text UNIQUE NOT NULL,       -- machine key, e.g. "linguist"
  name        text NOT NULL,              -- display name, e.g. "Linguist"
  description text NOT NULL,
  category    text NOT NULL,              -- "Research" | "Writing" | "Data" | "Design"
  upstream_url text NOT NULL,             -- full SSE URL, e.g. https://mcp-linguist.railway.app/sse
  upstream_api_key text NOT NULL DEFAULT '', -- API_KEY for the upstream server (empty = no auth)
  is_published boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);
```

### Existing: `oauth_clients.allowed_mcp_resources`

Already a `text[]` column. Will store slugs from `mcp_catalogue`, e.g. `["linguist", "writing-library"]`.

---

## Architecture

```
LLM (Claude / ChatGPT)
  │  SSE connection, Bearer token
  ▼
GET /gateway/{client_id}
  │  1. Validate Bearer token via provider.load_access_token()
  │  2. Load client's allowed_mcp_resources[] from oauth_clients
  │  3. Load matching rows from mcp_catalogue
  │  4. Expose 4 meta-tools via FastMCP SSE
  ▼
Gateway meta-tools:
  search_tools(query)     → keyword search across all enabled MCPs' tool names + descriptions
  list_mcps()             → list client's enabled MCPs with descriptions
  list_tools(mcp_slug)    → list tools for one MCP (fetched from upstream initialize)
  call_tool(mcp_slug, tool_name, arguments) → proxy call to upstream SSE, return result

Upstream MCP servers (SSE at /sse):
  mcp-linguist-production.up.railway.app/sse
  mcp-writing-library-production.up.railway.app/sse
  … etc
```

---

## Gateway Implementation

### Route: `GET /gateway/{client_id}`

- Reads `Authorization: Bearer <token>` header
- Validates token via `provider.load_access_token(token)` — must be active, not revoked, not expired
- Validates `at.client_id == client_id` (token belongs to this client)
- Loads `allowed_mcp_resources` from `oauth_clients` for the client
- Loads matching `mcp_catalogue` rows (published only)
- Creates a FastMCP instance with 4 tools and runs it as SSE

### Tool: `search_tools(query: str) → str`

Searches tool names and descriptions across all enabled MCPs. Tool metadata is fetched from each upstream server at gateway startup via MCP `initialize` + `tools/list`. Results are cached in-process per client session.

Returns JSON string: `[{"mcp": "linguist", "tool": "translate_text", "description": "..."}]`

### Tool: `list_mcps() → str`

Returns JSON string of enabled MCPs: `[{"slug": "linguist", "name": "Linguist", "description": "...", "category": "Writing"}]`

### Tool: `list_tools(mcp_slug: str) → str`

Returns JSON string of all tools for the given MCP slug: `[{"name": "translate_text", "description": "...", "parameters": {...}}]`

### Tool: `call_tool(mcp_slug: str, tool_name: str, arguments: dict) → str`

- Validates `mcp_slug` is in client's enabled list
- Connects to upstream SSE server, calls the tool via MCP protocol
- Returns tool result as JSON string
- Logs one row to `oauth_usage_logs`

### Upstream MCP client

Use `mcp` Python library (`mcp.client.sse.sse_client` + `mcp.client.session.ClientSession`) to connect to upstream SSE servers and call tools. Add `mcp` to `pyproject.toml` dependencies.

---

## Client Portal

### Auth

- `POST /portal/login` — form: `client_id` + `client_secret`
  - Validates via `get_client()` + `verify_secret()`
  - On success: sets signed session cookie `portal_session` containing `{"client_id": "..."}` using `itsdangerous.URLSafeTimedSerializer` with `SECRET_KEY` from settings
  - On failure: re-renders login with error
- `POST /portal/logout` — clears cookie, redirects to login
- All `/portal/*` routes check cookie via `_require_portal_client()` dependency

### Routes

| Route | Description |
|---|---|
| `GET /portal/login` | Login form (standalone, light card layout like register.html) |
| `POST /portal/login` | Auth handler |
| `GET /portal/` | Overview: client name, usage today/month/total, gateway URL |
| `GET /portal/mcps` | MCP catalogue with toggles showing current selection |
| `POST /portal/mcps` | Save MCP selection → updates `allowed_mcp_resources` |
| `GET /portal/setup` | Config download page |
| `GET /portal/setup/download` | Returns `claude_desktop_config.json` as file download |
| `POST /portal/logout` | Clear session, redirect to login |

### Templates

All portal templates extend a new `portal_base.html` — same CSS tokens as `base.html` but with a different sidebar (client-facing nav: Overview, My MCPs, Setup Guide, Sign Out). No admin nav links.

| Template | Notes |
|---|---|
| `portal_login.html` | Standalone light card (same pattern as `consent.html`) |
| `portal_base.html` | Sidebar layout, client nav |
| `portal_overview.html` | Usage stats, gateway URL copy box |
| `portal_mcps.html` | Catalogue grid with category badges and toggles |
| `portal_setup.html` | Config blocks (Claude Desktop + ChatGPT) + download button |

---

## Admin Catalogue

### Routes (added to `src/admin/routes.py`)

| Route | Description |
|---|---|
| `GET /admin/catalogue` | List all MCP catalogue entries |
| `GET /admin/catalogue/new` | Create form |
| `POST /admin/catalogue` | Create entry |
| `GET /admin/catalogue/{slug}/edit` | Edit form |
| `POST /admin/catalogue/{slug}/edit` | Save edits |
| `POST /admin/catalogue/{slug}/publish` | Toggle `is_published` |
| `POST /admin/catalogue/{slug}/delete` | Hard delete |

### Templates

| Template | Notes |
|---|---|
| `catalogue_list.html` | Table: name, category, URL, published status, actions |
| `catalogue_form.html` | Shared create/edit form |

### Sidebar nav update

Add "Catalogue" link to `base.html` sidebar between "Clients" and "Registrations".

---

## Config

Add to `src/config.py`:

```python
SECRET_KEY: str = "change-me-portal-secret"
```

Used for signing portal session cookies.

---

## New Files

| File | Responsibility |
|---|---|
| `src/portal/__init__.py` | Empty |
| `src/portal/routes.py` | All `/portal/*` routes + `_require_portal_client()` dependency |
| `src/gateway/__init__.py` | Empty |
| `src/gateway/routes.py` | `GET /gateway/{client_id}` SSE endpoint |
| `src/gateway/upstream.py` | Upstream MCP client: fetch tool list, call tool via SSE |
| `src/admin/templates/catalogue_list.html` | Admin catalogue list |
| `src/admin/templates/catalogue_form.html` | Admin catalogue create/edit form |
| `src/portal/templates/portal_login.html` | Client login |
| `src/portal/templates/portal_base.html` | Portal base layout |
| `src/portal/templates/portal_overview.html` | Overview tab |
| `src/portal/templates/portal_mcps.html` | MCP selection tab |
| `src/portal/templates/portal_setup.html` | Setup guide + config download |

---

## Router Registration (main.py)

```python
from src.portal.routes import router as portal_router
from src.gateway.routes import router as gateway_router

app.include_router(portal_router)
app.include_router(gateway_router)
```

---

## Verification

1. **Admin catalogue:** Add a test MCP entry (e.g. Linguist), publish it. Confirm it appears in portal.
2. **Portal login:** Visit `/portal/login`, log in with a valid client_id + client_secret. Confirm redirect to overview.
3. **MCP toggle:** Enable Linguist, save. Confirm `allowed_mcp_resources` updated in Supabase.
4. **Setup config:** Visit `/portal/setup`. Confirm gateway URL shown, claude_desktop_config.json block contains only enabled MCPs. Download works.
5. **Gateway SSE:** Connect Claude Desktop to `/gateway/{client_id}` with a valid access token. Confirm `list_mcps` returns enabled MCPs. Confirm `search_tools("translate")` returns linguist tools. Confirm `call_tool("linguist", "translate_text", {...})` returns a real translation.
6. **Usage logging:** After a `call_tool` call, confirm a row appears in `oauth_usage_logs` and the portal overview counter increments.
7. **Auth guard:** Confirm `/portal/` without cookie redirects to `/portal/login`. Confirm `/gateway/{client_id}` without Bearer token returns 401.

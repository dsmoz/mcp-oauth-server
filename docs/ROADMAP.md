# DSMOZ MCP OAuth Server — Roadmap

> Last updated: 2026-04-04

---

## Completed

### v1.0 — Core OAuth 2.0 Server
- Authorization Code + PKCE flow
- Token issuance, refresh, revocation
- Supabase-backed client registry and token store
- Basic admin panel (client list, create, revoke)
- HTTP Basic auth on admin routes
- Railway deployment (Docker, python:3.11-slim + uv)

### v1.1 — Telegram Approval Gate
Replaced browser password consent with mobile-first Telegram bot approval.

**Flow:**
1. `GET /authorize` → pending session created → redirect to `/authorize/consent?session={id}`
2. `GET /authorize/consent` → sends Telegram message with Approve/Deny inline buttons → renders "Waiting for approval..." page with 2s JS polling
3. Telegram bot receives `callback_query` → `POST /telegram/webhook` → calls `complete_authorization()` or marks session denied
4. `/consent/status?session={id}` → returns `pending | approved | denied | expired`
5. On approved: browser redirects to `redirect_uri?code=…&state=…`
6. `POST /token` unchanged — MCP client exchanges code for tokens

**Key files:**
- `src/telegram.py` — Telegram Bot API wrapper (plain httpx, no library)
- `src/config.py` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID`
- `src/oauth/routes.py` — `/consent/status`, `/telegram/webhook`
- `src/oauth/provider.py` — `mark_session_approved()`, `mark_session_denied()`, `update_session_telegram_id()`, in-process `_approved_redirects` store
- `main.py` — webhook registration on startup

**Fallback:** Password form still served if `TELEGRAM_BOT_TOKEN` is not configured.

**Duplicate guard:** Telegram message only sent once per session — checked via `telegram_message_id` in session JSON before sending.

### v1.2 — Admin Panel Enhancements + Self-Service Registration

**Admin dashboard** (`/admin/`):
- Stats grid: total clients, active clients, total tokens, pending registrations
- Recent clients table

**Client management** (`/admin/clients/`):
- Edit client name and redirect URIs (`GET/POST /admin/clients/{id}/edit`)
- Rotate secret (`POST /admin/clients/{id}/rekey`) — invalidates old secret, existing tokens unaffected
- Hard delete (`POST /admin/clients/{id}/delete`) — cascades to tokens and auth codes
- Token inspector (`GET /admin/clients/{id}/tokens`) — lists active tokens with fingerprint, issued/expiry dates, revoke-by-hash
- Bulk delete with checkboxes
- Portal credentials — admin can set/reset portal username and password per client

**Self-service registration** (public, no auth):
- `GET /register` — form: company name, contact name, email, use case, redirect URIs
- Anti-bot: honeypot field + timing check (rejects submissions under 3s)
- `POST /register/submit` — stores in `oauth_registration_requests` table, sends Telegram alert to owner
- `GET /register/success` — confirmation page

**Admin registration review** (`/admin/registrations/`):
- List with pending/approved/rejected badges
- Detail view with full request data
- `POST /admin/registrations/{id}/approve` — auto-creates OAuth client, shows secret once, redirects to client detail
- `POST /admin/registrations/{id}/reject` — marks rejected

**Database:**
- `oauth_registration_requests` table (id uuid, company_name, contact_name, contact_email, use_case, redirect_uris_raw, status check constraint, created_at, reviewed_at, reviewed_by)
- FK cascade constraints on `oauth_authorization_codes`, `oauth_access_tokens`, `oauth_refresh_tokens` → `oauth_clients(client_id) ON DELETE CASCADE`
- `oauth_clients` extended: `portal_username`, `portal_password_hash`, `allowed_mcp_resources[]`

### v1.3 — Visual Identity
- `docs/visual-identity-guide.html` — DSMOZ Intelligence brand guide
- Palette: DS-MOZ teal (`#115E67`), orange (`#FF5E00`), dark background (`#072B31`)
- Font: Avenir Next (locally confirmed)
- Icons: Phosphor Icons, Light variant

### v1.4 — RFC Compliance + Claude Desktop / Cursor Support
- `POST /register` — RFC 7591 dynamic client registration (Claude Desktop auto-registers)
- `GET /.well-known/oauth-protected-resource` — RFC 9728
- `token_endpoint_auth_methods_supported: ["client_secret_post", "none"]` — PKCE public clients
- `client_secret` optional on `/token` when `code_verifier` present
- Consent completion: confirmation page with auto-redirect + manual fallback button (works in embedded webviews)
- All "Claude Desktop" references replaced with generic "MCP client"

### v1.5 — Client Portal
Self-service portal for approved clients.

**Auth:** `itsdangerous` signed session cookie; one-time 24h setup link on approval; username/password thereafter.

**Pages:**
- `/portal` — login
- `/portal/setup-password?token=…` — first-login password setup
- `/portal/overview` — usage stats, gateway URL
- `/portal/mcps` — enable/disable MCPs from published catalogue
- `/portal/setup` — Claude Desktop / Cursor config, JSON download, copy buttons

**Database:** `portal_setup_tokens` table (client_id, token_hash, expires_at, used_at)

### v1.6 — DS-MOZ Intelligence Gateway
Single MCP endpoint per client that proxies to multiple upstream MCP servers.

**Endpoints:**
- `GET /gateway/{client_id}` — SSE stream (MCP server)
- `POST /gateway/{client_id}/messages` — MCP message channel

**Meta-tools exposed to LLM:**
- `list_mcps` — list enabled MCPs
- `browse_mcps` — browse all published MCPs with enabled status
- `add_mcp` — add MCP to client's toolbox (persists to DB)
- `remove_mcp` — remove MCP from toolbox
- `search_tools` — keyword search across enabled MCPs
- `list_tools` — list tools for a specific MCP
- `call_tool` — proxy tool call to upstream

**Upstream transport:** auto-detects SSE (`/sse`) vs streamable HTTP (`/mcp`)

**Auth:** Bearer token validated via `SupabaseOAuthProvider`; upstream MCPs use static `API_TOKENS` key (no OAuth introspection needed — gateway handles auth)

**Memory management:** idle transport eviction (4h TTL, 30min sweep) + immediate eviction on client revoke

### v1.7 — MCP Catalogue (Admin)
Admin-managed catalogue of available MCP servers.

- `GET /admin/catalogue` — lists Railway services via GraphQL API, cross-referenced with `mcp_catalogue` DB table
- Publish/unpublish toggle — clicking Publish on a Railway service upserts it into DB
- Auto-generates LLM-discovery descriptions by fetching tool list on publish
- Descriptions instruct LLM to use `search_tools`/`list_tools` before calling
- `mcp_catalogue` table: slug, name, description, category, upstream_url, upstream_api_key, is_published
- Requires: `RAILWAY_API_TOKEN`, `RAILWAY_PROJECT_ID` env vars

---

## Backlog

### Security
- [ ] Rate-limit `/register/submit` and `/consent/status` polling
- [ ] CSRF tokens on admin forms (currently rely on Basic auth + SameSite cookies)
- [ ] Audit log — record all admin actions with timestamp and actor
- [ ] Webhook secret verification — validate `X-Telegram-Bot-Api-Secret-Token` on `/telegram/webhook`

### Operational
- [ ] Token cleanup job — delete expired tokens on schedule (currently accumulate in DB)
- [ ] Multi-owner Telegram support — multiple chat IDs for approval requests
- [ ] Health endpoint — expose DB connectivity and Telegram webhook status
- [ ] Persist `_approved_redirects` to Supabase (currently in-process dict — lost on restart mid-flow)

### Gateway
- [ ] Upstream connection pooling — reuse HTTP connections to upstream MCPs (currently fresh connection per tool call, adds 200-500ms overhead)
- [ ] Tool call timeout — configurable per-MCP timeout to prevent slow upstreams blocking clients
- [ ] Circuit breaker — stop retrying a down upstream after N failures
- [ ] Multi-instance sticky sessions — required if Railway scales to 2+ replicas (needs Redis or Supabase-backed session state)

### Portal
- [ ] Usage charts — visual breakdown by MCP and time period
- [ ] Notification on new MCP published — email or in-portal alert

### Admin
- [ ] Flash messages on admin actions (success/error without page flicker)
- [ ] Pagination on clients list and token inspector
- [ ] `POST /admin/clients/{id}/rotate-all-tokens` — force re-auth for all sessions

---

## Architecture

```
MCP Client (Claude Desktop, Cursor, etc.)
     │
     │  Authorization Code + PKCE
     ▼
POST /register (RFC 7591 dynamic registration)
     │
     ▼
GET /authorize → pending session (Supabase)
     │
     ▼
GET /authorize/consent
     ├─ Telegram configured? ──YES──► send_approval_request() → consent_waiting.html
     │                                      │
     │                          Telegram callback_query
     │                                      │
     │                         POST /telegram/webhook
     │                                      │
     │                          mark_session_approved()
     │                                      │
     └─ No Telegram ──────────► password form (fallback)
          │
          ▼
GET /consent/status (polled every 2s)
     │  status: approved
     ▼
GET /consent/complete → confirmation page + auto-redirect attempt
     │
     ▼
POST /token → access_token + refresh_token
     │
     ▼
GET /gateway/{client_id}  ←── Bearer token
     │  SSE stream
     │  meta-tools: list_mcps, browse_mcps, add_mcp, remove_mcp,
     │              search_tools, list_tools, call_tool
     │
     ├──► upstream MCP 1 (SSE: /sse)
     ├──► upstream MCP 2 (streamable HTTP: /mcp)
     └──► upstream MCP N ...
```

## Configuration Reference

| Env var | Required | Description |
|---------|----------|-------------|
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Yes | Supabase service role key |
| `OAUTH_ISSUER_URL` | Yes | Public base URL (e.g. `https://mcp.dsmozconsultancy.com`) |
| `ADMIN_USERNAME` | Yes | HTTP Basic auth username for `/admin/` |
| `ADMIN_PASSWORD` | Yes | HTTP Basic auth password for `/admin/` |
| `SECRET_KEY` | Yes | Signing key for portal session cookies |
| `TELEGRAM_BOT_TOKEN` | Recommended | BotFather token — enables Telegram approval gate |
| `TELEGRAM_OWNER_CHAT_ID` | Recommended | Owner's Telegram chat ID |
| `RAILWAY_API_TOKEN` | Recommended | Railway API token — enables catalogue auto-discovery |
| `RAILWAY_PROJECT_ID` | Recommended | Railway project UUID — scopes catalogue to your project |
| `INTROSPECT_SECRET` | Yes | Shared secret for `/introspect` endpoint |
| `BREVO_API_KEY` | Optional | Brevo API key — enables approval email notifications |

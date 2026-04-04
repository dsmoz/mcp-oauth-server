# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [1.8.0] - 2026-04-04

### Added
- Self-service registration: `POST /register/submit` now creates the OAuth client immediately — no admin approval gate; credentials email sent via Brevo on submit
- Credit system: `credit_balance` on `oauth_clients`; `credit_cost_per_call` on `mcp_catalogue`; `credits_used` on `oauth_usage_logs`
- Credit gate in gateway `call_tool` — blocks calls when balance < cost; meta-tools (browse, add, remove, search) remain free
- Admin: "Add Credits" form on client detail page (`POST /admin/clients/{id}/add-credits`)
- Portal: `/portal/credits` — Buy Credits page with Starter / Pro / Enterprise plan cards (dummy payment, grants credits immediately)
- Portal: credit balance displayed on overview with "Buy Credits" shortcut button
- Email: DS-MOZ orange logo, Claude.ai web connector setup section, corrected Claude Desktop SSE config format

### Changed
- Registration flow: pending/approval queue replaced by instant account creation
- `oauth_registration_requests` auto-approved on submit (status=approved, reviewed_by=self-service)
- Portal sidebar: "My MCPs" renamed to "My Toolbox" with toolbox icon
- `OAUTH_ISSUER_URL` updated to `https://mcp.dsmozconsultancy.com`
- Claude Desktop email config corrected from broken `npx` command to proper `type: sse` remote format

### Fixed
- New clients pre-populated with all published MCPs on dynamic registration

## [1.7.0] - 2026-04-04

### Added
- Admin MCP catalogue (`/admin/catalogue`) — lists Railway services via GraphQL API, cross-referenced with `mcp_catalogue` DB table
- Publish/unpublish toggle — upserts Railway service into DB on first publish
- Auto-generated LLM-discovery descriptions by fetching tool list on publish
- `mcp_catalogue` table: slug, name, description, category, upstream_url, upstream_api_key, is_published

## [1.6.0] - 2026-04-04

### Added
- DS-MOZ Intelligence Gateway: single MCP endpoint per client proxying to multiple upstream MCP servers
- `GET /gateway/{client_id}` — SSE stream; `POST /gateway/{client_id}/messages` — MCP message channel
- Meta-tools: `list_mcps`, `browse_mcps`, `add_mcp`, `remove_mcp`, `search_tools`, `list_tools`, `call_tool`
- Auto-detect upstream transport: SSE (`/sse`) vs streamable HTTP (`/mcp`)
- Idle transport eviction (4h TTL, 30min sweep) + immediate eviction on client revoke
- Bearer token auth on gateway; upstream MCPs use static `API_TOKENS` key

## [1.5.0] - 2026-04-04

### Added
- Client portal: `/portal/` — self-service portal for approved clients
- Portal auth: `itsdangerous` signed session cookie; one-time 24h setup link; username/password thereafter
- Pages: login, setup-password, overview (usage stats + gateway URL), MCPs selection, setup guide
- `portal_setup_tokens` table

## [1.4.0] - 2026-04-04

### Added
- `POST /register` — RFC 7591 dynamic client registration (Claude Desktop / Cursor auto-registers)
- `GET /.well-known/oauth-protected-resource` — RFC 9728
- `token_endpoint_auth_methods_supported: ["client_secret_post", "none"]` — PKCE public clients
- Consent completion: confirmation page with auto-redirect + manual fallback (works in embedded webviews)

## [1.3.0] - 2026-04-04

### Added
- Visual identity: DSMOZ teal/orange/dark palette, Avenir Next font, Phosphor Icons

## [1.2.0] - 2026-04-04

### Added
- Admin dashboard stats grid, recent clients table
- Client edit, secret rotate, hard delete, token inspector, bulk delete
- Portal credentials (username + password) per client
- Self-service registration form (`GET/POST /register`) with anti-bot checks
- Admin registration review: list, detail, approve, reject
- `oauth_registration_requests` table; FK cascade constraints; `portal_username`, `portal_password_hash`, `allowed_mcp_resources[]` on `oauth_clients`

## [1.1.0] - 2026-04-04

### Added
- Telegram approval gate replacing browser password consent
- `GET /authorize/consent` — sends Telegram message with Approve/Deny buttons; renders polling page
- `POST /telegram/webhook` — receives callback_query, calls `complete_authorization()` or denies
- `GET /consent/status` — polls pending/approved/denied/expired
- Duplicate guard: Telegram message sent only once per session

### Changed
- Password form kept as fallback when `TELEGRAM_BOT_TOKEN` is not configured

## [1.0.0] - 2026-01-17

### Added
- Authorization Code + PKCE flow
- Token issuance, refresh, revocation
- Supabase-backed client registry and token store
- Basic admin panel (client list, create, revoke)
- HTTP Basic auth on admin routes
- Railway deployment (Docker, python:3.11-slim + uv)

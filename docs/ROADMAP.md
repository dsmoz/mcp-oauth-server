# DSMOZ MCP OAuth Server — Roadmap

> Last updated: 2026-04-04

---

## Implemented

### v1.0 — Core OAuth 2.0 Server ✅
- Authorization Code + PKCE flow
- Token issuance, refresh, revocation (RFC 7009)
- Supabase-backed client registry and token store
- Basic admin panel (client list, create, revoke)
- HTTP Basic auth on admin routes
- Railway deployment (Docker, python:3.11-slim + uv)

### v1.1 — Telegram Approval Gate ✅
Replaced browser password consent with mobile-first Telegram bot approval.

- `GET /authorize/consent` → sends Telegram message with Approve/Deny inline buttons → renders polling page
- `POST /telegram/webhook` → callback_query → `complete_authorization()` or denied
- `GET /consent/status` → pending | approved | denied | expired
- Duplicate guard: Telegram message sent only once per session
- Password form fallback when `TELEGRAM_BOT_TOKEN` not configured

### v1.2 — Admin Panel Enhancements + Self-Service Registration ✅
- Dashboard stats grid: total/active clients, tokens, pending registrations
- Client edit, secret rotate, hard delete, token inspector, bulk delete
- Portal credentials (username + password) per client
- Self-service registration form with anti-bot (honeypot + timing)
- Admin registration review: list, detail, approve, reject
- `oauth_registration_requests` table; FK cascade constraints

### v1.3 — Visual Identity ✅
- DS-MOZ teal (#115E67), orange (#FF5E00), dark background (#072B31)
- Avenir Next font, Phosphor Icons (light variant)
- Applied across admin, portal, email templates

### v1.4 — RFC Compliance + Claude Desktop / Cursor Support ✅
- `POST /register` — RFC 7591 dynamic client registration
- `GET /.well-known/oauth-protected-resource` — RFC 9728
- `token_endpoint_auth_methods_supported: ["client_secret_post", "none"]`
- Consent completion page with auto-redirect + manual fallback (embedded webviews)

### v1.5 — Client Portal ✅
- Portal auth: signed session cookie; one-time 24h setup link; username/password thereafter
- Pages: login, setup-password, overview (usage stats + gateway URL), MCPs, setup guide
- `portal_setup_tokens` table

### v1.6 — DS-MOZ Intelligence Gateway ✅
- `GET /gateway/{client_id}` — SSE stream; `POST /gateway/{client_id}/messages` — message channel
- Meta-tools: `list_mcps`, `browse_mcps`, `add_mcp`, `remove_mcp`, `search_tools`, `list_tools`, `call_tool`
- Auto-detect upstream transport: SSE vs streamable HTTP
- Idle transport eviction (4h TTL, 30min sweep) + immediate eviction on token revoke
- Bearer token auth; upstream MCPs use static `API_TOKENS`

### v1.7 — MCP Catalogue (Admin) ✅
- `/admin/catalogue` — Railway GraphQL auto-discovery, cross-referenced with `mcp_catalogue` DB
- Publish/unpublish toggle — upserts on first publish
- Auto-generated LLM-discovery descriptions on publish
- `mcp_catalogue` table: slug, name, description, category, upstream_url, upstream_api_key, is_published, credit_cost_per_call

### v1.8 — Simplified Onboarding + Credit System ✅
- Registration: instant client creation (no admin approval gate); credentials email via Brevo
- Credit system: `credit_balance` on clients, `credit_cost_per_call` on catalogue, `credits_used` on usage logs
- Credit gate in `call_tool`; meta-tools remain free
- Admin add-credits form; portal Buy Credits page (dummy payment — Starter/Pro/Enterprise)
- Credit balance on portal overview
- "My Toolbox" rename across portal
- Email: DS-MOZ orange logo, Claude.ai web connector section, corrected SSE Desktop config

---

## Backlog

### Monetization

#### Real Payment Integration — Priority: High
- [ ] Stripe (or equivalent) integration on `POST /portal/credits/buy`
- [ ] Payment webhook endpoint (`POST /webhooks/stripe`) with signature verification
- [ ] Transaction log table (`credit_transactions`: client_id, amount, plan, payment_id, status, created_at)
- [ ] Invoice PDF generation on purchase (Weasyprint or similar)
- [ ] Invoice email delivery with download link
- [ ] Tax/VAT handling (country-based rate lookup)

#### Billing & Subscriptions — Priority: Medium
- [ ] Trial credits on new registration (configurable amount, e.g. 5 free credits)
- [ ] Credit expiry: `expires_at` on credit grants; job to zero out expired credits
- [ ] Subscription plans: recurring billing, auto-renewal, cancellation flow
- [ ] Low-balance email alert (threshold configurable per client)
- [ ] Spending reports: client-facing usage breakdown by MCP, tool, date range
- [ ] Admin billing dashboard: total revenue, MRR, credit sales by plan

### Client Management

#### Password & Account Security — Priority: High
- [ ] Password reset flow: `GET/POST /portal/forgot-password` → token email → new password form
- [ ] Password reset token table (same pattern as `portal_setup_tokens`)
- [ ] Email verification on registration (confirm email before credentials sent)

#### Client Lifecycle — Priority: Medium
- [ ] Client suspension with reason and notification email (distinguish from revoke)
- [ ] Client reactivation flow (admin UI + email notification)
- [ ] Account deletion (GDPR-compliant: purge PII, keep anonymised usage data)
- [ ] Contact update flow (email change with re-verification)

#### Usage & Reporting — Priority: Medium
- [ ] Portal usage charts (calls by MCP, calls over time — Chart.js or similar)
- [ ] Usage export: CSV download from portal and admin
- [ ] Admin usage overview across all clients
- [ ] Quota management: per-client monthly call limit with soft/hard cutoffs

#### Multi-User Organisations — Priority: Low
- [ ] User table linked to client_id (multiple logins per org)
- [ ] Roles: Admin (full portal access) / Viewer (read-only stats)
- [ ] Invite by email → setup token flow
- [ ] Audit trail: who changed what in the portal

#### API Key Management — Priority: Low
- [ ] API key table as alternative to OAuth token flow
- [ ] Key generation, labelling, rotation, revocation
- [ ] Scope/permission per key
- [ ] Portal key management UI

### Security

- [ ] Rate-limit `/register/submit` and `/consent/status` polling (e.g. 5 req/min per IP)
- [ ] CSRF tokens on admin forms (currently rely on Basic auth + SameSite cookies)
- [ ] Audit log table — record all admin actions with timestamp, actor, resource, action
- [ ] Webhook secret verification — validate `X-Telegram-Bot-Api-Secret-Token` on `/telegram/webhook`
- [ ] Email verification before account activation

### Operational

- [ ] Token cleanup job — delete expired tokens on schedule (currently accumulate in DB)
- [ ] Health endpoint — expose DB connectivity, Telegram webhook status, gateway transport count
- [ ] Persist `_approved_redirects` to Supabase (currently in-process dict — lost on restart)
- [ ] Multi-instance sticky sessions — Redis or Supabase-backed (required if Railway scales to 2+ replicas)
- [ ] Error alerting — Sentry or similar for uncaught exceptions and email delivery failures
- [ ] Structured request logging with correlation IDs

### Gateway

- [ ] Upstream connection pooling — reuse HTTP connections (currently fresh per tool call, 200-500ms overhead)
- [ ] Tool call timeout — configurable per-MCP to prevent slow upstreams blocking clients
- [ ] Circuit breaker — stop retrying a down upstream after N failures
- [ ] Per-client rate limiting on `call_tool`

### Portal

- [ ] Usage charts — visual breakdown by MCP and time period
- [ ] Notification on new MCP published — in-portal alert + optional email
- [ ] Onboarding checklist on first login (connect MCP client, add first tool, make first call)

### Admin

- [ ] Flash messages on admin actions (success/error feedback without page flicker)
- [ ] Pagination on clients list and token inspector
- [ ] `POST /admin/clients/{id}/rotate-all-tokens` — force re-auth for all sessions
- [ ] Admin usage dashboard — calls today/month across all clients, top MCPs, revenue

### Developer Experience

- [ ] OpenAPI/Swagger served at `/docs` (FastAPI native — just needs enabling)
- [ ] Sandbox/test mode flag — mock upstream MCPs, no real credit deductions
- [ ] Client webhook subscriptions — notify clients on events (new MCP, low credits, etc.)
- [ ] Quickstart guide / documentation site

---

## Architecture

```
MCP Client (Claude.ai, Claude Desktop, Cursor, ChatGPT)
     │
     │  Self-service Registration → Instant credentials email
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
     ├─ Telegram configured? ──YES──► Telegram approval → POST /telegram/webhook
     └─ No Telegram ──────────────► password form (fallback)
          │
          ▼
GET /consent/status (polled every 2s) → approved
     │
     ▼
POST /token → access_token + refresh_token
     │
     ▼
GET /gateway/{client_id}  ←── Bearer token
     │  SSE stream
     │  meta-tools (free): list_mcps, browse_mcps, add_mcp, remove_mcp,
     │                      search_tools, list_tools
     │  credit-gated:       call_tool → credit check → deduct → upstream
     │
     ├──► upstream MCP 1 (SSE: /sse)
     ├──► upstream MCP 2 (streamable HTTP: /mcp)
     └──► upstream MCP N ...

Client Portal (/portal/)
     ├─ Overview — credit balance, usage stats, gateway URL
     ├─ My Toolbox — enable/disable MCPs
     ├─ Setup Guide — config snippets
     └─ Credits — Buy Credits (Starter / Pro / Enterprise)
```

## Configuration Reference

| Env var | Required | Description |
|---------|----------|-------------|
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Yes | Supabase service role key |
| `OAUTH_ISSUER_URL` | Yes | Public base URL (`https://mcp.dsmozconsultancy.com`) |
| `ADMIN_USERNAME` | Yes | HTTP Basic auth username for `/admin/` |
| `ADMIN_PASSWORD` | Yes | HTTP Basic auth password for `/admin/` |
| `SECRET_KEY` | Yes | Signing key for portal session cookies |
| `INTROSPECT_SECRET` | Yes | Shared secret for `/introspect` endpoint |
| `TELEGRAM_BOT_TOKEN` | Recommended | Enables Telegram approval gate |
| `TELEGRAM_OWNER_CHAT_ID` | Recommended | Owner's Telegram chat ID |
| `BREVO_API_KEY` | Recommended | Brevo API key — enables credentials email |
| `BREVO_SENDER_EMAIL` | Recommended | Sender email address |
| `BREVO_SENDER_NAME` | Optional | Sender display name (default: DS-MOZ Intelligence) |
| `RAILWAY_API_TOKEN` | Recommended | Railway API token — enables catalogue auto-discovery |
| `RAILWAY_PROJECT_ID` | Recommended | Railway project UUID |

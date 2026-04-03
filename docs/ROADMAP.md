# DSMOZ MCP OAuth Server — Roadmap

> Last updated: 2026-04-03

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
6. `POST /token` unchanged — Claude Desktop exchanges code for tokens

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

**Self-service registration** (public, no auth):
- `GET /register` — form: company name, contact name, email, use case, redirect URIs
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

**Templates added:**
- `dashboard.html`, `registrations_list.html`, `registration_detail.html`
- `client_edit.html`, `client_tokens.html`
- `consent_waiting.html`, `register.html`, `register_success.html`

**Jinja2 filter:** `unix_to_date` registered on `templates.env.filters` for token timestamps.

**Bug fix:** Replaced all `maybe_single()` calls (raises `APIError` on zero rows in supabase-py v2) with `.limit(1).execute()` helper pattern.

### v1.3 — Visual Identity
- `docs/visual-identity-guide.html` — DSMOZ Intelligence brand guide
- Palette: DS-MOZ teal (`#115E67`), orange (`#FF5E00`), dark background (`#072B31`)
- Font: Avenir Next (locally confirmed)
- Icons: Phosphor Icons, Light variant
- Self-contained HTML, no external dependencies

---

## Backlog

### UX Polish
- [ ] Apply DSMOZ Intelligence visual identity to all admin and public-facing templates (replace current violet/neutral-dark scheme with teal/orange brand)
- [ ] Flash messages (success/error) on admin actions without page flicker
- [ ] Pagination on clients list and token inspector for large datasets

### Security
- [ ] Rate-limit `/register/submit` (prevent spam registrations)
- [ ] Rate-limit `/consent/status` polling endpoint
- [ ] CSRF tokens on all admin forms (currently rely on Basic auth + SameSite cookies)
- [ ] Audit log table — record all admin actions with timestamp and actor

### Operational
- [ ] Token cleanup job — delete expired tokens on a schedule (currently accumulate in DB)
- [ ] Webhook secret verification — validate `X-Telegram-Bot-Api-Secret-Token` header on `/telegram/webhook`
- [ ] Health endpoint (`/health`) exposing DB connectivity and Telegram webhook status
- [ ] Multi-owner support — allow multiple Telegram chat IDs to receive approval requests

### Developer Experience
- [ ] OpenAPI docs at `/docs` (currently disabled in production)
- [ ] `POST /admin/clients/{id}/rotate-all-tokens` — force all active sessions to re-auth
- [ ] CLI seed script for local dev (create test client without browser)

---

## Configuration Reference

| Env var | Required | Description |
|---------|----------|-------------|
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Supabase service role key |
| `OAUTH_ISSUER_URL` | Yes | Public base URL (e.g. `https://mcp-oauth-server.up.railway.app`) |
| `ADMIN_PASSWORD` | Yes | HTTP Basic auth password for `/admin/` |
| `TELEGRAM_BOT_TOKEN` | Recommended | BotFather token — enables Telegram approval gate |
| `TELEGRAM_OWNER_CHAT_ID` | Recommended | Owner's Telegram chat ID — get via `/start` with the bot |

---

## Architecture

```
Claude Desktop
     │
     │  Authorization Code + PKCE
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
redirect_uri?code=…&state=…
     │
     ▼
POST /token → access_token + refresh_token
```

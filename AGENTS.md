---
title: DS-MOZ Connect — MCP OAuth Server & Gateway
date_created: 2026-06-12
date_modified: 2026-06-12
version: 1.0
modified_by_agent: Claude Code
keywords_and_tags:
  - mcp-oauth-server
  - oauth2-pkce
  - mcp-gateway
  - multi-tenant
  - fastapi
  - supabase
  - portal
  - billing
---

# DS-MOZ Connect — MCP OAuth Server & Gateway

**Read this file before scanning the project folder.**

OAuth 2.0 authorization server + AI gateway for Claude Desktop, Claude.ai, and Cursor.
Full Authorization Code + PKCE flow, self-service client registration, a client portal,
and a single gateway endpoint that proxies many upstream MCP servers per user.

**Live:** `https://mcp.dsmozconsultancy.com`
**Supabase:** `bwbghsnnrszdcmwqzjwv` (dsmoz-intel) — service-role key, server-side only.

## Layout

| Path | Role |
| ---- | ---- |
| `main.py` | FastAPI app entry; mounts oauth, gateway, portal, admin routers. |
| `src/oauth/` | OAuth 2.0 provider (`/authorize`, `/token`, `/revoke`, introspection). |
| `src/gateway/` | Gateway: meta-tools + upstream proxy. `routes.py` (MCP traffic), `rest_proxy.py` (`/api/plugin/*` → mcp-scholar), `upstream.py`. |
| `src/portal/` | Server-rendered client portal (FastAPI + Jinja2). `routes.py` (session cookie auth via `_require_portal_user`), `templates/`. |
| `src/admin/` | Admin console (catalogue, users, billing). |
| `src/users/` | User + agent-token providers. |
| `src/integrations/` | Microsoft Graph etc. |
| `migrations/` | Dated SQL migrations against the Supabase project. |

## Billing contract (CRITICAL)

The **gateway is the sole biller.** Upstream MCPs MUST NOT call `/introspect` with
`cost > 0` and MUST NOT ship credit logic. LLM-using upstream calls report usage via
`_meta.usage_usd` (preferred) or `_meta.llm = {model, input_tokens, output_tokens}`.
See `~/Documents/Programming/mcp-oauth-server/docs/saas-mcp-development-guide.md` and
the pricing-model reference before changing any cost path.

## Auth model

- **Portal routes** (`/portal/*`): signed-cookie session, keyed on `user_id`
  (`_require_portal_user`).
- **Gateway / REST proxy**: bearer credential → `(user_id, client_id)`. Two credential
  types: `dsmoz_*` agent tokens and OAuth `access_token`s.
- **Upstream tenancy key is `X-User-ID`** (= portal `user_id`). `X-Client-ID` is gateway
  telemetry (e.g. `mc_…`), NOT a tenancy key.
- `rest_proxy.py` injects `X-MCP-Credentials` (base64 user config) on every proxied call.
  Endpoints that must NOT trigger upstream auto-provision (status probes) bypass
  rest_proxy and call upstream with admin token + `X-User-ID` and no credentials —
  see `src/portal/routes.py` `_scholar_call`.

## Conventions

- Migrations: dated `migrations/YYYY-MM-DD_name.sql`, applied to Supabase `bwbghsnnrszdcmwqzjwv`.
- RLS: tables are RLS-enabled with no `authenticated` policies — all access is service-role.
  Do not add permissive `USING(true)` policies.
- Secrets: `.env.local` (dev) / Railway env (prod). Never commit `.env*`.
- Deployment: Railway; Sentry for errors.

## Sibling repos

Upstream MCP servers live in `~/Documents/Programming/mcp-servers/<name>/`
(`mcp-scholar`, `mcp-loom`, etc.). Each has its own `AGENTS.md`/`CLAUDE.md`.

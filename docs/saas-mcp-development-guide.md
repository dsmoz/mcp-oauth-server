# SaaS MCP Development Guide

**Audience:** developers building new MCP servers that will be exposed through the DS-MOZ Connect gateway (`connect.dsmozconsultancy.com`) and monetized per call.

**Scope:** end-to-end blueprint covering authentication middleware, per-user context propagation, credit deduction, gateway registration, and operational checklists. Read this before writing any new upstream MCP.

---

## 1. Architecture Overview

```
end user (Claude Desktop / portal)
        │  Bearer <oauth_access_token>
        ▼
┌────────────────────────────────┐
│ mcp-oauth-server (gateway+IDP) │  ← issues tokens, owns user/credit DB,
│  connect.dsmozconsultancy.com  │    exposes /introspect (atomic deduction)
└──────────────┬─────────────────┘
               │  proxied request +
               │  X-User-Token: <raw oauth token>
               │  X-User-ID:    <user uuid>
               │  X-MCP-Credentials: <base64 json>
               ▼
┌────────────────────────────────┐
│   Upstream MCP (your server)   │  ← validates token via /introspect,
│   *-production.up.railway.app  │    deducts credit before each LLM/API call
└────────────────────────────────┘
```

Key facts:

- **Gateway is the single source of truth** for users and credit balance.
- **Upstream MCPs never touch the user table** — they only call `/introspect`.
- **Credits deducted at the upstream**, atomically, per LLM call (academia model).
- **Token never leaves Railway** — gateway forwards the raw token via `X-User-Token` header so upstreams can introspect.

---

## 2. Authentication Tiers

Every upstream MCP must implement the same four-tier `BearerAuthMiddleware`. Tiers tried in order; first hit wins:

| Tier | Trigger | Use case |
|------|---------|----------|
| **0. Direct client auth** | `X-Client-ID` + `X-Client-Token` headers | Direct API consumers (no OAuth) |
| **1. Per-client API token** | `Authorization: Bearer <token>` matches `clients.api_token` | Legacy / direct integrations |
| **2. OAuth introspection** | `Authorization: Bearer <token>` validated via `/introspect` | All gateway-routed traffic (default) |
| **3. Admin/gateway token** | Token in `API_TOKENS` env CSV | Internal admin tools, gateway proxy |

Reference implementation: [`mcp-scholar/src/auth/middleware.py`](../../mcp-servers/mcp-scholar/src/auth/middleware.py). Copy-and-adapt this file for any new MCP.

### 2.1 Required env vars (per upstream)

```
OAUTH_ISSUER_URL=https://connect.dsmozconsultancy.com
INTROSPECT_SECRET=<shared secret — match oauth-server>
API_TOKENS=<comma-separated admin tokens, optional>
OAUTH_INTROSPECT_TIMEOUT=3.0
OAUTH_INTROSPECT_CACHE_TTL=60
CREDIT_COST_PER_LLM_CALL=<3.0 | 5.0>   # cost per LLM/API call
```

### 2.2 Headers the gateway forwards

| Header | Purpose |
|--------|---------|
| `Authorization: Bearer <token>` | OAuth access token (raw) |
| `X-User-Token` | Same token, exposed to upstream for introspection |
| `X-User-ID` | UUID of the authenticated user (post-multi-device migration) |
| `X-Client-ID` | Legacy tenancy hint (still accepted) |
| `X-MCP-Credentials` | Base64-encoded JSON of user-supplied provider credentials (API keys, etc.) |

Upstreams MUST honour `X-User-ID` first (user-level tenancy) and fall back to `X-Client-ID` only for legacy callers.

---

## 3. Credit Deduction (Monetization)

### 3.1 The contract

The oauth-server exposes `POST /introspect`:

```json
// request
{ "token": "<raw_oauth_token>", "cost": 3.0, "upstream": "mcp-yourname" }
// header: x-introspect-secret: <INTROSPECT_SECRET>

// response (success)
{ "active": true, "user_id": "...", "credits_remaining": 12.5 }

// response (insufficient)
{ "active": false, "reason": "insufficient_credits" }

// response (billing system error)
{ "active": false, "reason": "billing_error" }
```

Deduction is **atomic** server-side via the `deduct_credits_user` Postgres RPC. There is no race window.

### 3.2 Required `credit.py` module

Drop this verbatim into `src/credit.py` (or `tools/credit.py`) of every new MCP. Adjust default cost to match the cost table:

```python
"""Per-LLM-call credit deduction via mcp-oauth-server /introspect."""
import os
from contextvars import ContextVar

import requests

# Set by BearerAuthMiddleware from X-User-Token header — read by deduct_credits.
current_user_token: ContextVar[str | None] = ContextVar("current_user_token", default=None)

COST_PER_LLM_CALL: float = float(os.getenv("CREDIT_COST_PER_LLM_CALL", "3.0"))


def deduct_credits(upstream_name: str = "mcp-yourname") -> bool:
    """Deduct credits before an LLM/API call. Return True to proceed, False to abort.

    Fail-open policy:
      - missing token (stdio/dev mode)        → allow
      - cost <= 0                              → allow
      - misconfigured (no URL/secret)          → allow
      - HTTP error / timeout                   → allow (don't block on infra)
    Fail-closed:
      - oauth-server returns active=false      → reject
    """
    token = current_user_token.get()
    if not token:
        return True
    cost = COST_PER_LLM_CALL
    if cost <= 0:
        return True
    base_url = os.getenv("OAUTH_ISSUER_URL", "").rstrip("/")
    secret = os.getenv("INTROSPECT_SECRET", "").strip()
    if not base_url or not secret:
        return True
    try:
        resp = requests.post(
            f"{base_url}/introspect",
            json={"token": token, "cost": cost, "upstream": upstream_name},
            headers={"x-introspect-secret": secret},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return True
        return resp.json().get("active", False)
    except Exception:
        return True
```

### 3.3 Wiring `current_user_token` from middleware

In `BearerAuthMiddleware.__call__`, after extracting headers, set the ContextVar so `deduct_credits()` can read it deeper in the call stack:

```python
from src.credit import current_user_token

user_token_header = headers.get(b"x-user-token", b"").decode().strip() or None
ctx_user_token = current_user_token.set(user_token_header)
try:
    await self.app(scope, receive, send)
finally:
    current_user_token.reset(ctx_user_token)
```

### 3.4 Where to place the guard

**Place the guard on the actual API-calling function, not on routers/dispatchers.** One LLM call = one deduction. Do NOT guard CRUD / DB-only endpoints.

```python
# Correct — guard at the LLM call site
async def stream_llm_response(prompt: str):
    if not deduct_credits():
        yield f"data: {json.dumps({'error': 'Insufficient credits'})}\n\n"
        return
    # ... actually call OpenAI/Anthropic/etc.
```

Patterns by response type:

- **Streaming (SSE)**: yield an `error` event then `return`.
- **JSON**: `raise HTTPException(status_code=402, detail="Insufficient credits")` (HTTP 402 Payment Required).
- **Plain function**: return a sentinel error result your caller already handles.

### 3.5 Cost table (current production values)

| MCP | Cost / call | Reasoning |
|-----|-------------|-----------|
| mcp-linguist | 3.0 | Single Anthropic translate call |
| mcp-scholar | 3.0 | Single Anthropic chat call |
| mcp-loom | 5.0 | Multi-step LLM orchestration |
| mcp-dsmoz-nexus | 5.0 | Multi-step retrieval + chat |
| mcp-design-engine | 5.0 | Image/video generation API |
| mcp-academia | 0.0 | No direct LLM calls (delegates to linguist) |
| mcp-surveylab | 0.0 | Pure stats — numpy/pandas |

New MCPs default to **3.0** unless they orchestrate multiple model calls or hit expensive paid APIs.

---

## 4. Gateway Registration

Once your upstream is deployed and credit-gated:

1. **Generate an upstream API key** (32 random bytes, base64) — this is the token the gateway uses to authenticate as itself when proxying. Add it to your upstream's `API_TOKENS` env var.
2. **In the admin panel** (`/admin/catalogue/new`), register the MCP:
   - **Slug**: short machine key (`linguist`, `scholar`, …) — immutable.
   - **Display Name** + **Description** (use "Generate Description" after first save).
   - **Category** + **Tier** (standard or super).
   - **Upstream URL**: `https://mcp-<slug>-production.up.railway.app/mcp`.
   - **API Key**: paste the upstream API key.
   - **Credit Cost Per Call**: must match the upstream's `CREDIT_COST_PER_LLM_CALL`. This value is informational for the user (gateway does not enforce it — the upstream does, atomically).
   - **Config Schema**: JSON array of credential fields the user must fill in (see [`per-user-mcp-credentials.md`](per-user-mcp-credentials.md) for schema format).
3. **Test end-to-end**: enable the MCP from `/portal/toolbox`, invoke a tool, check that credit balance decreases and `oauth_usage_logs` records the call.

---

## 5. Operational Checklists

### 5.1 Pre-deploy checklist (new MCP)

- [ ] `BearerAuthMiddleware` copied + adapted; supports all 4 tiers.
- [ ] `credit.py` module present with correct `COST_PER_LLM_CALL` default.
- [ ] All LLM/expensive-API call sites guarded with `if not deduct_credits(): ...`.
- [ ] `current_user_token.set(...)` wired in middleware.
- [ ] Health endpoint at `/health` (skipped by middleware).
- [ ] Tests cover: insufficient credits → reject, no token → allow (stdio mode), introspect 5xx → allow.
- [ ] No direct DB writes to `users.credit_balance` from upstream — only via `/introspect`.

### 5.2 Railway env vars (per upstream)

```bash
railway variables --service mcp-<slug> \
  --set "OAUTH_ISSUER_URL=https://connect.dsmozconsultancy.com" \
  --set "INTROSPECT_SECRET=<shared_secret>" \
  --set "CREDIT_COST_PER_LLM_CALL=<3.0|5.0>" \
  --set "API_TOKENS=<gateway_upstream_token>"
```

Then `railway redeploy --service mcp-<slug>` to pick up.

### 5.3 Admin onboarding checklist

- [ ] Register MCP via `/admin/catalogue/new`.
- [ ] Set Telegram bot settings (`/admin/settings` → Telegram Bot) so registration/topup notifications work.
- [ ] Smoke test: enable MCP from a test user's `/portal/toolbox`, invoke a free meta-tool (`list_mcp_tools`), then a paid tool, verify deduction.

---

## 6. Anti-Patterns (Don't Do This)

- **Direct DB credit deduction** from upstream — defeats atomicity, races with gateway.
- **Caching `/introspect` results past 60s** without honouring the token's `exp` — security risk.
- **Guarding cheap endpoints** (CRUD, listings, health) — degrades UX, wastes credits.
- **Failing closed on introspect timeouts** — outages on the oauth-server should not cascade to all upstreams.
- **Storing user credentials in DB** when the user already supplies them per-call via `X-MCP-Credentials` — use the gateway-forwarded creds instead.
- **Bearer token in query string** — leaks into logs/referrers. Headers only.

---

## 7. References

- [`per-user-oauth-billing.md`](per-user-oauth-billing.md) — billing integration deep dive.
- [`per-user-mcp-credentials.md`](per-user-mcp-credentials.md) — credential storage / config schema.
- Reference implementations:
  - [`mcp-scholar`](../../mcp-servers/mcp-scholar/) — full BearerAuthMiddleware + credit guards.
  - [`mcp-linguist`](../../mcp-servers/mcp-linguist/) — minimal upstream pattern.
- Code locations in this repo:
  - Gateway forwarding: `src/gateway/routes.py`
  - `/introspect` endpoint: `src/oauth/routes.py`
  - Atomic deduction RPC: see `migrations/2026-05-15_credit_costs_and_topup_requests.sql`

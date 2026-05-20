# SaaS MCP Development Guide

**Audience:** developers building new MCP servers that will be exposed through the DS-MOZ Connect gateway (`connect.dsmozconsultancy.com`) and monetized per call.

**Scope:** end-to-end blueprint covering authentication middleware, per-user context propagation, LLM-usage reporting, gateway registration, and operational checklists. Read this before writing any new upstream MCP.

**Pricing model (v1, 2026-05-20):** all pricing & cost-formula details live in
[`_ADMIN/Reference-Documents/2026-05-20_saas-mcp-pricing-model.md`](../../OneDrive-dsmozconsultancy.com/Consultancies/_ADMIN/Reference-Documents/2026-05-20_saas-mcp-pricing-model.md).
This guide MUST be read together with that doc — single source of truth.

---

## 1. Architecture Overview

```
end user (Claude Desktop / portal)
        │  Bearer <oauth_access_token | dsmoz_*>
        ▼
┌────────────────────────────────┐
│ mcp-oauth-server (gateway+IDP) │  ← issues tokens, owns user/credit DB,
│  connect.dsmozconsultancy.com  │    SOLE biller (post-call settlement)
└──────────────┬─────────────────┘
               │  proxied request +
               │  X-User-Token: <raw bearer>
               │  X-User-ID:    <user uuid>
               │  X-MCP-Credentials: <base64 json>
               ▼
┌────────────────────────────────┐
│   Upstream MCP (your server)   │  ← validates token via /introspect,
│   *-production.up.railway.app  │    reports LLM usage via _meta on response
└────────────────────────────────┘
```

Key facts:

- **Gateway is the single source of truth** for users and credit balance.
- **Gateway is the SOLE biller.** Upstreams MUST NOT deduct credits — neither directly nor via `/introspect`. Any cost field sent to `/introspect` is ignored by the IDP.
- **Cost is computed post-call** by the gateway from observed Railway compute (duration × vCPU rate), egress bytes, plus any LLM usage the upstream reports via response `_meta`.
- **Upstream MCPs never touch the user table** — they only call `/introspect` to validate the token (returns active/inactive + user_id).
- **Tokens never leave Railway** — gateway forwards the raw token via `X-User-Token` header so upstreams can introspect.

---

## 2. Authentication Tiers

Every upstream MCP must implement the same four-tier `BearerAuthMiddleware`. Tiers tried in order; first hit wins:

| Tier | Trigger | Use case |
|------|---------|----------|
| **0. Direct client auth** | `X-Client-ID` + `X-Client-Token` headers | Direct API consumers (no OAuth) |
| **1. Per-client API token** | `Authorization: Bearer <token>` matches `clients.api_token` | Legacy / direct integrations |
| **2. OAuth introspection** | `Authorization: Bearer <token>` validated via `/introspect` | All gateway-routed traffic (default) |
| **3. Admin/gateway token** | Token in `API_TOKENS` env CSV | Internal admin tools, gateway proxy |

`/introspect` transparently accepts both OAuth access tokens and agent tokens (`dsmoz_*`). Upstreams don't need to branch on token format.

Reference implementation: [`mcp-scholar/src/auth/middleware.py`](../../mcp-servers/mcp-scholar/src/auth/middleware.py). Copy-and-adapt for any new MCP.

### 2.1 Required env vars (per upstream)

```
OAUTH_ISSUER_URL=https://connect.dsmozconsultancy.com
INTROSPECT_SECRET=<shared secret — match oauth-server>
API_TOKENS=<comma-separated admin tokens, optional>
OAUTH_INTROSPECT_TIMEOUT=3.0
OAUTH_INTROSPECT_CACHE_TTL=60
```

`CREDIT_COST_PER_LLM_CALL` is **deprecated and ignored**. Remove it from new MCPs.

### 2.2 Headers the gateway forwards

| Header | Purpose |
|--------|---------|
| `Authorization: Bearer <token>` | OAuth access token or agent token (raw) |
| `X-User-Token` | Same token, exposed to upstream for introspection |
| `X-User-ID` | UUID of the authenticated user |
| `X-Client-ID` | Legacy tenancy hint (still accepted) |
| `X-MCP-Credentials` | Base64-encoded JSON of user-supplied provider credentials (API keys, etc.) |

Upstreams MUST honour `X-User-ID` first (user-level tenancy) and fall back to `X-Client-ID` only for legacy callers.

---

## 3. Billing — Reporting Usage to the Gateway

### 3.1 The contract (single-biller pattern)

The gateway bills post-call. After your tool returns, the gateway computes:

```
raw_usd       = compute_usd + egress_usd + llm_usd + base + surcharge
sell_usd      = raw_usd × (1 + withholding) × (1 + margin) × (1 + iva)
credits       = sell_usd / usd_per_credit
```

`compute_usd` and `egress_usd` come from the observed request duration + response size. **You don't need to do anything for these.**

`llm_usd` is OPTIONAL but recommended. Without it the gateway only bills compute (cheap). For LLM-heavy MCPs, report usage via the response `_meta` field — see 3.2.

### 3.2 `_meta` reporting contract

When your tool calls one or more LLMs, attach a `_meta` block to the response JSON. Two equivalent forms; pick whichever is easier:

**Form A — token counts (preferred):**

```python
return {
    "content": [...],
    "_meta": {
        "llm": {
            "model": "claude-sonnet-4-6",
            "input_tokens": 1240,
            "output_tokens": 380,
            "cached_input_tokens": 0,  # optional
        },
    },
}
```

The gateway looks up `model_prices` and computes `llm_usd` itself. List of priced models is in the `model_prices` Supabase table and editable via `/admin/cost-model/models`.

**Form B — USD passthrough:**

```python
return {
    "content": [...],
    "_meta": {"usage_usd": 0.0127},  # exact LLM cost in USD
}
```

Use this when the model isn't priced yet, or when you've already paid an upstream provider (e.g. OpenRouter returned a USD cost in the response).

If both fields are present, `usage_usd` wins.

### 3.3 Multi-step LLM orchestration

If your tool makes several LLM calls before responding, sum the costs and report once in `_meta`. Either:

- Aggregate token counts per model into `_meta.llm` (if all calls used the same model), OR
- Set `_meta.usage_usd` to the total USD spend.

### 3.4 Errors

If your tool returns an error response (`isError: true` or `{"error": "..."}`), the gateway **does not bill**. Free tries on errors are intentional — don't game it by silently swallowing exceptions.

### 3.5 What NOT to do

- ❌ Do not import a `credit.py` module that calls `/introspect` with a `cost` field — that field is now ignored, but the call itself wastes a round-trip.
- ❌ Do not write to `users.credit_balance` directly.
- ❌ Do not hard-code a flat `CREDIT_COST_PER_LLM_CALL` env var.
- ❌ Do not refuse to serve on insufficient credits — the gateway's pre-call balance check handles that.

---

## 4. Gateway Registration

Once your upstream is deployed:

1. **Generate an upstream API key** (32 random bytes, base64) — gateway uses this to authenticate as itself when proxying. Add it to your upstream's `API_TOKENS` env var.
2. **In the admin panel** (`/admin/catalogue/new`), register the MCP:
   - **Slug**: short machine key (`linguist`, `scholar`, …) — immutable.
   - **Display Name** + **Description** (use "Generate Description" after first save).
   - **Category** + **Tier** (standard or super).
   - **Upstream URL**: `https://mcp-<slug>-production.up.railway.app/mcp`.
   - **API Key**: paste the upstream API key.
   - **Config Schema**: JSON array of credential fields the user must fill in (see [`per-user-mcp-credentials.md`](per-user-mcp-credentials.md) for schema format).
   - `credit_cost_per_call` column is **deprecated** — leave at 0; gateway computes cost from the formula.
3. **Cost profile** (optional): in `/admin/cost-model/profiles`, attach a `compute_rate_name` (e.g. `high-mem` for memory-heavy services) and a `fixed_surcharge_usd` (e.g. for paid API passthroughs not modeled as tokens).
4. **Test end-to-end**: enable from `/portal/toolbox`, invoke a tool, verify `oauth_usage_logs` records `compute_usd`, `llm_usd`, `sell_usd`, `credits_charged`.

---

## 5. Operational Checklists

### 5.1 Pre-deploy checklist (new MCP)

- [ ] `BearerAuthMiddleware` copied + adapted; supports all 4 tiers.
- [ ] `current_user_token` ContextVar wired from `X-User-Token` header.
- [ ] All LLM call sites attach `_meta.llm` or `_meta.usage_usd` to the response.
- [ ] Errors return `isError: true` (gateway skips billing on errors).
- [ ] Health endpoint at `/health` (skipped by middleware).
- [ ] Tests cover: introspect 200/active → allow, introspect inactive → reject, no token → allow (stdio mode), `_meta` propagated to response.
- [ ] **No `credit.py` self-deduction code.** No `CREDIT_COST_PER_LLM_CALL` env var.

### 5.2 Railway env vars (per upstream)

```bash
railway variables --service mcp-<slug> \
  --set "OAUTH_ISSUER_URL=https://connect.dsmozconsultancy.com" \
  --set "INTROSPECT_SECRET=<shared_secret>" \
  --set "API_TOKENS=<gateway_upstream_token>"
```

Then `railway redeploy --service mcp-<slug>` to pick up.

### 5.3 Migration of an existing MCP from flat-fee to formula model

1. Delete `src/credit.py` (or `tools/credit.py`).
2. Remove all `if not deduct_credits(): ...` guards.
3. Remove `CREDIT_COST_PER_LLM_CALL` and `INTROSPECT_SECRET` from your tool-call logic (`INTROSPECT_SECRET` stays in env for token validation by middleware, just not consulted for deduction).
4. Attach `_meta.llm` (or `_meta.usage_usd`) to every tool response that involves LLM work.
5. Set `mcp_catalogue.credit_cost_per_call = 0` for your slug.
6. Redeploy.

---

## 6. Anti-Patterns (Don't Do This)

- **Self-deduction via `/introspect`** — gateway is the sole biller. `cost` field is ignored by the IDP. Remove this code.
- **Direct DB credit writes** from upstream — defeats atomicity, races with gateway.
- **Caching `/introspect` results past 60s** without honouring the token's `exp` — security risk.
- **Guarding cheap endpoints** (CRUD, listings, health) — gateway's pre-call balance check is enough.
- **Failing closed on introspect timeouts** — outages on the oauth-server should not cascade to all upstreams.
- **Storing user credentials in DB** when the user already supplies them per-call via `X-MCP-Credentials` — use gateway-forwarded creds instead.
- **Bearer token in query string** — leaks into logs/referrers. Headers only.
- **Swallowing errors silently** to avoid `isError: true` — that breaks free-tries-on-errors and is dishonest. Return proper error responses.

---

## 7. References

- [Pricing model v1 (2026-05-20)](../../OneDrive-dsmozconsultancy.com/Consultancies/_ADMIN/Reference-Documents/2026-05-20_saas-mcp-pricing-model.md) — formula, tax stack, package design, change log.
- [`per-user-oauth-billing.md`](per-user-oauth-billing.md) — billing integration deep dive.
- [`per-user-mcp-credentials.md`](per-user-mcp-credentials.md) — credential storage / config schema.
- Reference implementations:
  - [`mcp-scholar`](../../mcp-servers/mcp-scholar/) — full BearerAuthMiddleware + `_meta` reporting.
- Code locations in this repo:
  - Gateway billing: `src/gateway/billing.py`
  - Gateway forwarding + post-call settlement: `src/gateway/routes.py`
  - `/introspect` endpoint: `src/oauth/routes.py`
  - Pricing migration: `migrations/2026-05-20_pricing_model.sql`

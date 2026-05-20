# mcp-oauth-server (DS-MOZ Connect)

OAuth 2.1 IDP + multi-tenant gateway in front of all DS-MOZ MCP servers. Hosted at `connect.dsmozconsultancy.com`.

## Building new upstream MCPs

**REQUIRED READING (both):**

1. [`docs/saas-mcp-development-guide.md`](docs/saas-mcp-development-guide.md) — auth + ContextVar plumbing, gateway header forwarding (`X-User-Token`, `X-User-ID`, `X-MCP-Credentials`), Railway env vars, admin catalogue registration, anti-patterns.
2. **Pricing model:** `~/Library/CloudStorage/OneDrive-dsmozconsultancy.com/Consultancies/_ADMIN/Reference-Documents/2026-05-20_saas-mcp-pricing-model.md` — single source of truth for credit cost formula, schema (`pricing_config`, `compute_rates`, `model_prices`, `mcp_cost_profile`), Mozambique tax stack, and upstream MCP contract (NO self-deduction; gateway is sole biller).

Any new MCP added to the catalogue MUST follow both before deployment. Upstream MCPs MUST NOT call `/introspect` with `cost > 0` and MUST NOT ship a `src/credit.py`. LLM-using calls report usage via `_meta.usage_usd` or `_meta.llm` in the response body — see the pricing model doc for the contract.

## Related docs

- [`docs/per-user-oauth-billing.md`](docs/per-user-oauth-billing.md) — billing integration
- [`docs/per-user-mcp-credentials.md`](docs/per-user-mcp-credentials.md) — credential storage / config schema
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — product roadmap
- [`docs/sop-mcp-catalogue-descriptions.md`](docs/sop-mcp-catalogue-descriptions.md) — SOP for writing human-readable catalogue descriptions

## Key paths

- Gateway proxy: `src/gateway/routes.py`
- OAuth + introspect: `src/oauth/routes.py` (atomic credit deduction via `deduct_credits_user` RPC)
- Admin panel: `src/admin/routes.py` + `src/admin/templates/`
- Portal (user-facing): `src/portal/routes.py` + `src/portal/templates/`
- Telegram notifications: `src/telegram.py` (DB settings → env fallback)
- Migrations: `migrations/*.sql` (apply via Supabase MCP `apply_migration`)

## UI conventions

**Prefer icons over text links for row actions and affordances.** Edit, Delete, Revoke, Copy, Connect, Disconnect, Re-link, Refresh — render as compact ghost icon buttons, not as text links or large colored buttons. Use `title=`/`aria-label=` for accessibility. Reference pattern: commit `285d49b` (M365 connect: ghost icon buttons + chip status).

- **Portal** (`src/portal/templates/`) — lucide-style inline SVG icons (matches `portal_base.html` + `portal_mcps.html`). Use the `.btn` family with an icon-only child SVG. No CDN needed.
- **Admin** (`src/admin/templates/`) — Phosphor icons via the existing CDN (`<i class="ph-light ph-*">`). Use `ph-pencil-simple` (edit), `ph-trash` (delete), `ph-copy`, `ph-arrow-clockwise` (refresh), `ph-plug` / `ph-plug-x` (connect/disconnect).
- **Public marketing pages** (`portal_landing.html`, `public_catalog.html`) — lucide via CDN (`<i data-lucide="...">`), already wired.

**Keep text labels for:** form-primary actions (Save, Submit, Add X), navigation Cancel buttons, and standalone CTAs ("Top up", "Manage gateway").

## Commit conventions

- `feat(scope): ...` — new feature
- `fix(scope): ...` — bug fix
- `docs(scope): ...` — documentation only
- Scopes: `gateway`, `oauth`, `admin`, `portal`, `credits`, `telegram`, `clients`

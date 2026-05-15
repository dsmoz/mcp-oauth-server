# mcp-oauth-server (DS-MOZ Connect)

OAuth 2.1 IDP + multi-tenant gateway in front of all DS-MOZ MCP servers. Hosted at `connect.dsmozconsultancy.com`.

## Building new upstream MCPs

**REQUIRED READING:** [`docs/saas-mcp-development-guide.md`](docs/saas-mcp-development-guide.md)

Covers: BearerAuthMiddleware (4-tier auth), `current_user_token` ContextVar, `credit.py` template, `/introspect` contract, gateway header forwarding (`X-User-Token`, `X-User-ID`, `X-MCP-Credentials`), cost table, Railway env vars, admin catalogue registration, anti-patterns.

Any new MCP added to the catalogue MUST follow this guide before deployment.

## Related docs

- [`docs/per-user-oauth-billing.md`](docs/per-user-oauth-billing.md) — billing integration
- [`docs/per-user-mcp-credentials.md`](docs/per-user-mcp-credentials.md) — credential storage / config schema
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — product roadmap

## Key paths

- Gateway proxy: `src/gateway/routes.py`
- OAuth + introspect: `src/oauth/routes.py` (atomic credit deduction via `deduct_credits_user` RPC)
- Admin panel: `src/admin/routes.py` + `src/admin/templates/`
- Portal (user-facing): `src/portal/routes.py` + `src/portal/templates/`
- Telegram notifications: `src/telegram.py` (DB settings → env fallback)
- Migrations: `migrations/*.sql` (apply via Supabase MCP `apply_migration`)

## Commit conventions

- `feat(scope): ...` — new feature
- `fix(scope): ...` — bug fix
- `docs(scope): ...` — documentation only
- Scopes: `gateway`, `oauth`, `admin`, `portal`, `credits`, `telegram`, `clients`

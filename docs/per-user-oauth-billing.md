# Per-User OAuth Billing Integration

How to make a downstream app (Next.js, Python, anything HTTP) call an upstream MCP server using **the end user's own Connect credits**, not a shared service account.

This is the pattern used by `dsmoz-academia` → `mcp-linguist`. Copy it for any new MCP that needs per-user metered billing.

---

## Architecture

```
┌──────────────┐                ┌────────────────────┐               ┌─────────────────┐
│              │  1. /authorize │                    │               │                 │
│   App        │ ─────────────► │  Connect           │               │  Upstream MCP   │
│ (Next.js)    │ ◄───────────── │  (mcp-oauth-server)│               │  (e.g. linguist)│
│              │  2. code+token │                    │               │                 │
└──────┬───────┘                └─────────▲──────────┘               └────────▲────────┘
       │                                  │                                   │
       │ 3. fetch w/ Bearer <user_token>  │ 4. POST /introspect               │
       └──────────────────────────────────┼───────────────────────────────────┘
                                          │   {token, cost, upstream, units}
                                          │   header: x-introspect-secret
                                          │
                                          ▼
                              Atomic credit deduction
                              (Supabase RPC deduct_credits_user)
```

**Trust model.** Connect holds the user balance and is the **only** place that decides whether the call is allowed. The upstream MCP never touches the credits table directly — it asks Connect to introspect the bearer **and** deduct in one round trip. Fail-closed on any introspect/billing error.

**Three actors:**

| Actor | Role |
|-------|------|
| **Connect** (`mcp-oauth-server`) | OAuth 2.0 AS, introspect endpoint, credit ledger |
| **App** (e.g. `dsmoz-academia`) | OAuth client, stores per-user tokens, forwards bearer to upstream |
| **Upstream MCP** (e.g. `mcp-linguist`) | Resource server; validates bearer + deducts credits via introspect |

---

## Step 1 — Register a public OAuth client in Connect

A **public client** is a single `client_id` that many end-users can authorize. Tokens are bound to the **user_id captured at consent time**, not to whoever first claimed the client.

### Migration (already applied to Connect)

`migrations/2026-05-14_public_oauth_clients.sql`:

```sql
ALTER TABLE oauth_clients
    ADD COLUMN IF NOT EXISTS is_public_client boolean NOT NULL DEFAULT false;

ALTER TABLE oauth_authorization_codes
    ADD COLUMN IF NOT EXISTS user_id text;

-- Defence in depth: public clients must never be claimed by a single user.
CREATE OR REPLACE FUNCTION enforce_public_client_unclaimed()
RETURNS trigger AS $$
BEGIN
    IF NEW.is_public_client AND NEW.user_id IS NOT NULL THEN
        RAISE EXCEPTION 'public OAuth clients must not have user_id set';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_public_client_unclaimed
    BEFORE INSERT OR UPDATE ON oauth_clients
    FOR EACH ROW EXECUTE FUNCTION enforce_public_client_unclaimed();
```

### Register a row for your app

Insert into Connect's `oauth_clients`:

```sql
INSERT INTO oauth_clients (
  client_id, client_secret, name, redirect_uris,
  is_public_client, user_id
) VALUES (
  'mc_<random16bytes>',           -- public-facing identifier
  '<random32bytes>',              -- client secret (server-side only)
  'dsmoz-academia',
  ARRAY[
    'https://your-app.example.com/auth/connect/callback',
    'http://localhost:3000/auth/connect/callback'
  ],
  true,                           -- public_client
  NULL                            -- enforced by trigger above
);
```

Save `client_id` + `client_secret` — they go into the app's env (see Step 3).

---

## Step 2 — Upstream MCP: introspect + deduct

The upstream MCP (e.g. `mcp-linguist`) must:

1. Require an `Authorization: Bearer …` header on billed endpoints.
2. On each billed request, POST to `Connect/introspect` with the bearer **and** the credit cost.
3. Map the response to HTTP status: `active=true` → proceed; `active=false, reason=insufficient_credits` → 402; any error → fail closed.

### Introspect contract

`POST {CONNECT_ISSUER_URL}/introspect`

Headers:
```
Content-Type: application/json
x-introspect-secret: <shared INTROSPECT_SECRET>
```

Body:
```json
{
  "token": "<bearer access token>",
  "cost": 0.123,            // OPTIONAL. credits to deduct atomically. omit = no deduction.
  "upstream": "linguist",   // OPTIONAL. caller name, logged for analytics
  "units": 123              // OPTIONAL. metric (chars / tokens / pages) — logged only
}
```

Responses:

| HTTP | Body | Meaning |
|------|------|---------|
| 200  | `{"active": true, "user_id": "...", "client_id": "...", "credits_remaining": 99.5}` | Authorised; credits deducted if `cost > 0`. |
| 200  | `{"active": false, "reason": "insufficient_credits"}` | User is broke. Don't serve. |
| 200  | `{"active": false, "reason": "billing_error"}` | Supabase RPC failure. Fail closed. |
| 200  | `{"active": false}` | Token revoked / expired / unknown. |
| 403  | `{"detail": "forbidden"}` | Wrong `x-introspect-secret`. |

Atomic semantics: deduction uses `deduct_credits_user` Supabase RPC. Either credits are taken **and** `active=true`, or neither — no partial state.

### Reference implementation (Python / FastAPI)

`mcp-linguist/api/app.py`:

```python
import os, requests
from fastapi import HTTPException, Header

CREDIT_COST_PER_CHAR = float(os.getenv("LINGUIST_CREDIT_COST_PER_CHAR", "0"))

def _oauth_introspect_url() -> str:
    explicit = os.getenv("OAUTH_INTROSPECT_URL", "").strip()
    if explicit:
        return explicit
    issuer = os.getenv("OAUTH_ISSUER_URL", "").strip().rstrip("/")
    return f"{issuer}/introspect" if issuer else ""

def _is_static_token(token: str) -> bool:
    """Static service tokens (X-Api-Key path) bypass per-user billing."""
    env = os.getenv("API_TOKENS", "")
    return bool(env) and token in [t.strip() for t in env.split(",") if t.strip()]

def _deduct_for_translate(authorization: str | None, char_count: int) -> None:
    if CREDIT_COST_PER_CHAR <= 0:
        return  # billing disabled
    if not authorization or not authorization.lower().startswith("bearer "):
        return
    token = authorization[7:].strip()
    if not token or _is_static_token(token):
        return  # service token — not per-user

    introspect_url = _oauth_introspect_url()
    secret = os.getenv("INTROSPECT_SECRET", "").strip()
    if not introspect_url or not secret:
        return

    cost = round(char_count * CREDIT_COST_PER_CHAR, 6)
    if cost <= 0:
        return

    try:
        resp = requests.post(
            introspect_url,
            json={
                "token": token,
                "cost": cost,
                "upstream": "linguist",
                "units": char_count,
            },
            headers={"x-introspect-secret": secret},
            timeout=float(os.getenv("OAUTH_INTROSPECT_TIMEOUT", "3.0")),
        )
        data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail={
            "error": "billing_unreachable", "message": str(exc),
        })

    if data.get("active"):
        return  # paid, proceed

    reason = data.get("reason") or "unauthorized"
    if reason == "insufficient_credits":
        raise HTTPException(status_code=402, detail={
            "error": "insufficient_credits",
            "credits_required": cost,
            "topup_url": "https://connect.dsmozconsultancy.com/portal/credits",
        })
    raise HTTPException(status_code=402, detail={"error": reason})


@app.post("/translate/text")
async def translate(
    body: TranslateRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    _deduct_for_translate(authorization, len(body.text))
    return await _do_translate(body)
```

### Bearer auth middleware

Already handled by Connect's gateway pattern. For a standalone MCP, validate the bearer via the same `/introspect` (with `cost=None` for cheap validation, then a second call with `cost>0` after the work succeeds — or do both in one call before the work, accepting that aborted work is non-refundable).

The reference middleware in `mcp-linguist/http_server.py` accepts **either** static `API_TOKENS` (service-to-service) **or** OAuth bearer (per-user) — pick what suits the deployment.

---

## Step 3 — App: OAuth client + token storage

App side needs four pieces:

1. **Auth Code + PKCE flow** (`/auth/connect/start` → `/authorize` → `/auth/connect/callback` → `/token`).
2. **Encrypted token storage** keyed by app user_id (AES-256-GCM).
3. **Token refresh** with skew window.
4. **Bearer forwarding** to upstream MCP on every call.

### Database

```sql
-- supabase/migrations/0020_user_oauth_links.sql
CREATE TABLE user_oauth_links (
  user_id           uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  provider          text NOT NULL,          -- 'connect'
  remote_user_id    text,                   -- Connect user_id from /introspect
  access_token_enc  text NOT NULL,          -- AES-256-GCM ciphertext
  refresh_token_enc text,
  expires_at        timestamptz,
  scope             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, provider)
);

ALTER TABLE user_oauth_links ENABLE ROW LEVEL SECURITY;

-- Users can see they have a link; ciphertext is useless to them.
CREATE POLICY "own_link_select" ON user_oauth_links
  FOR SELECT USING (auth.uid() = user_id);
-- No INSERT/UPDATE/DELETE policies — all writes go through service role.
```

### Token encryption (AES-256-GCM)

```ts
// src/lib/oauth/encryption.ts
import crypto from 'node:crypto';

const ALGO = 'aes-256-gcm';

function getKey(): Buffer {
  const raw = process.env.OAUTH_LINK_ENCRYPTION_KEY;
  if (!raw) throw new Error('OAUTH_LINK_ENCRYPTION_KEY not configured');
  const key = Buffer.from(raw, 'base64');
  if (key.length !== 32) throw new Error(`key must be 32 bytes; got ${key.length}`);
  return key;
}

export function encryptToken(plaintext: string): string {
  const key = getKey();
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv(ALGO, key, iv);
  const ct = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  return `${iv.toString('base64')}:${cipher.getAuthTag().toString('base64')}:${ct.toString('base64')}`;
}

export function decryptToken(stored: string): string {
  const key = getKey();
  const [ivB64, tagB64, ctB64] = stored.split(':');
  const decipher = crypto.createDecipheriv(ALGO, key, Buffer.from(ivB64, 'base64'));
  decipher.setAuthTag(Buffer.from(tagB64, 'base64'));
  const pt = Buffer.concat([decipher.update(Buffer.from(ctB64, 'base64')), decipher.final()]);
  return pt.toString('utf8');
}
```

Generate the key once: `openssl rand -base64 32` → set as `OAUTH_LINK_ENCRYPTION_KEY` env.

### PKCE helpers

```ts
// src/lib/oauth/pkce.ts
import crypto from 'node:crypto';

const b64url = (b: Buffer) =>
  b.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

export const generateCodeVerifier = () => b64url(crypto.randomBytes(48));
export const codeChallenge = (v: string) =>
  b64url(crypto.createHash('sha256').update(v).digest());
export const randomState = () => b64url(crypto.randomBytes(24));
```

### OAuth client wrapper

```ts
// src/lib/oauth/connect.ts
export const CONNECT_ISSUER_URL =
  process.env.CONNECT_ISSUER_URL ?? 'https://connect.dsmozconsultancy.com';
export const CONNECT_CLIENT_ID = process.env.CONNECT_CLIENT_ID ?? '';
export const CONNECT_CLIENT_SECRET = process.env.CONNECT_CLIENT_SECRET ?? '';

export function authorizeUrl({ state, codeChallenge, scope }: {
  state: string; codeChallenge: string; scope?: string;
}): string {
  const url = new URL(`${CONNECT_ISSUER_URL}/authorize`);
  url.searchParams.set('client_id', CONNECT_CLIENT_ID);
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('redirect_uri', getRedirectUri());
  url.searchParams.set('code_challenge', codeChallenge);
  url.searchParams.set('code_challenge_method', 'S256');
  url.searchParams.set('state', state);
  if (scope) url.searchParams.set('scope', scope);
  return url.toString();
}

export async function exchangeCode({ code, codeVerifier }: {
  code: string; codeVerifier: string;
}): Promise<TokenResponse> {
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: CONNECT_CLIENT_ID,
    code, redirect_uri: getRedirectUri(), code_verifier: codeVerifier,
  });
  if (CONNECT_CLIENT_SECRET) body.set('client_secret', CONNECT_CLIENT_SECRET);
  return postToken(body);
}

export async function refreshAccessToken(refreshToken: string): Promise<TokenResponse> {
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    client_id: CONNECT_CLIENT_ID,
    refresh_token: refreshToken,
  });
  if (CONNECT_CLIENT_SECRET) body.set('client_secret', CONNECT_CLIENT_SECRET);
  return postToken(body);
}
```

### Start route (`/auth/connect/start`)

```ts
export async function GET() {
  const session = await getSession();
  if (!session) return redirect('/login?next=/conta/perfil');

  const state = randomState();
  const verifier = generateCodeVerifier();
  const challenge = codeChallenge(verifier);

  const res = NextResponse.redirect(
    authorizeUrl({ state, codeChallenge: challenge, scope: 'linguist' })
  );

  // Store state + verifier in httpOnly cookies for callback verification.
  const opts = { httpOnly: true, secure: true, sameSite: 'lax' as const, path: '/', maxAge: 600 };
  (await cookies()).set('connect_state', state, opts);
  (await cookies()).set('connect_verifier', verifier, opts);
  return res;
}
```

### Callback (`/auth/connect/callback`)

```ts
export async function GET(req: Request) {
  const session = await getSession();
  if (!session) return redirect('/login');

  const { code, state } = parseQuery(req);
  const cookieState = (await cookies()).get('connect_state')?.value;
  const cookieVerifier = (await cookies()).get('connect_verifier')?.value;

  if (cookieState !== state) return errorRedirect('state_mismatch');

  const tokens = await exchangeCode({ code, codeVerifier: cookieVerifier });

  // Optional: introspect immediately to capture remote_user_id for display.
  let remoteUserId: string | null = null;
  try {
    const intr = await introspectToken(tokens.access_token);
    if (intr.active && intr.user_id) remoteUserId = intr.user_id;
  } catch { /* non-fatal */ }

  await upsertConnectLink({
    userId: session.user.id,
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token ?? null,
    expiresIn: tokens.expires_in ?? null,
    scope: tokens.scope ?? null,
    remoteUserId,
  });

  return successRedirect();
}
```

### Token store with auto-refresh

```ts
// src/lib/oauth/token-store.ts
const REFRESH_SKEW_SECONDS = 60;

export async function getValidConnectToken(userId: string): Promise<string | null> {
  const db = createServiceRoleClient();
  const { data: row } = await db.from('user_oauth_links')
    .select('access_token_enc, refresh_token_enc, expires_at, scope, remote_user_id')
    .eq('user_id', userId).eq('provider', 'connect').maybeSingle();
  if (!row) return null;

  const expiresAt = row.expires_at ? Date.parse(row.expires_at) : null;
  const stillFresh = !expiresAt || expiresAt - Date.now() > REFRESH_SKEW_SECONDS * 1000;
  if (stillFresh) return decryptToken(row.access_token_enc);

  // Refresh path
  if (!row.refresh_token_enc) return null;
  try {
    const t = await refreshAccessToken(decryptToken(row.refresh_token_enc));
    await upsertConnectLink({
      userId,
      accessToken: t.access_token,
      refreshToken: t.refresh_token ?? decryptToken(row.refresh_token_enc),
      expiresIn: t.expires_in ?? null,
      scope: t.scope ?? row.scope,
      remoteUserId: row.remote_user_id,
    });
    return t.access_token;
  } catch {
    return null;  // refresh failed — caller should prompt re-link
  }
}
```

### Call the upstream MCP with the user's bearer

```ts
// src/lib/linguist.ts
const LINGUIST_ERRORS = {
  NOT_LINKED: 'CONNECT_NOT_LINKED',
  REAUTH: 'CONNECT_REAUTH_REQUIRED',
  CREDITS: 'INSUFFICIENT_CREDITS',
} as const;

export async function translateText(text: string, domain: string, userId: string): Promise<string> {
  const token = await getValidConnectToken(userId);
  if (!token) throw new Error(LINGUIST_ERRORS.NOT_LINKED);

  let res = await fetch(`${LINGUIST_API_URL}/api/translate/text`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      'X-Client-ID': LINGUIST_CLIENT_ID,
    },
    body: JSON.stringify({ text, sourceLang: 'pt', targetLang: 'en', domain }),
  });

  if (res.status === 401) {
    // Stale despite expiry check — token revoked upstream. Clear + prompt re-link.
    await clearConnectLink(userId);
    throw new Error(LINGUIST_ERRORS.REAUTH);
  }
  if (res.status === 402) throw new Error(LINGUIST_ERRORS.CREDITS);
  if (!res.ok) throw new Error(`Linguist ${res.status}: ${await res.text()}`);

  return (await res.json()).translation;
}
```

---

## Environment variables — full inventory

### Connect (mcp-oauth-server)
| Var | Purpose |
|-----|---------|
| `INTROSPECT_SECRET` | Shared secret upstream MCPs send in `x-introspect-secret` header. Rotate periodically. |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Credit ledger access. |

### Upstream MCP (e.g. mcp-linguist)
| Var | Value | Purpose |
|-----|-------|---------|
| `OAUTH_INTROSPECT_URL` | `https://connect.dsmozconsultancy.com/introspect` | Full introspect URL (or set `OAUTH_ISSUER_URL` and let `/introspect` be appended). |
| `INTROSPECT_SECRET` | **Same value as Connect's** | Server-to-server auth on `/introspect`. |
| `LINGUIST_CREDIT_COST_PER_CHAR` | `0.001` | Per-unit credit price. Set to `0` to disable billing. |
| `API_TOKENS` | comma-list | Optional: static service tokens that bypass per-user billing. |
| `OAUTH_INTROSPECT_TIMEOUT` | `3.0` | Seconds before /introspect call fails closed. |

### App (e.g. dsmoz-academia)
| Var | Value | Purpose |
|-----|-------|---------|
| `CONNECT_ISSUER_URL` | `https://connect.dsmozconsultancy.com` | OAuth AS root. **Note:** issuer is `connect.dsmozconsultancy.com`, not `mcp.dsmozconsultancy.com`. |
| `CONNECT_CLIENT_ID` | from `oauth_clients` row | App's public OAuth client_id. |
| `CONNECT_CLIENT_SECRET` | from `oauth_clients` row | Sent on `/token` exchange. |
| `OAUTH_LINK_ENCRYPTION_KEY` | `openssl rand -base64 32` | 32-byte base64 key for AES-GCM token storage. **Lose this = all linked users must re-auth.** |
| `LINGUIST_API_URL` | `https://mcp-linguist-production.up.railway.app` | Upstream MCP root. Client appends `/api/...`. |
| `LINGUIST_CLIENT_ID` | e.g. `__academia` | Glossary scope identifier sent in `X-Client-ID`. |
| `NEXT_PUBLIC_SITE_URL` | `https://your-app.example.com` | Used to compute `redirect_uri`. Must match a `redirect_uris[]` entry. |

---

## Smoke test (end-to-end)

```bash
# 1. Connect introspect endpoint reachable
curl -X POST https://connect.dsmozconsultancy.com/introspect \
  -H "Content-Type: application/json" \
  -H "x-introspect-secret: $INTROSPECT_SECRET" \
  -d '{"token":"faketoken"}'
# Expect: {"active":false}

# 2. Upstream MCP health
curl https://mcp-linguist-production.up.railway.app/api/health
# Expect: {"status":"ok",...}

# 3. Upstream MCP rejects unauthenticated request
curl -X POST https://mcp-linguist-production.up.railway.app/api/translate/text \
  -H "Authorization: Bearer faketoken" \
  -H "Content-Type: application/json" \
  -d '{"text":"olá","sourceLang":"pt","targetLang":"en"}'
# Expect: 401 {"error":"invalid_token",...}

# 4. Browser flow
#   a. Log into the app
#   b. Visit /conta/perfil → click "Ligar Connect"
#   c. Consent at connect.dsmozconsultancy.com
#   d. Trigger a translate action
#   e. Verify credits dropped at https://connect.dsmozconsultancy.com/portal/credits
```

---

## Reusing this for a new MCP

Checklist when adding per-user billing to a new MCP (call it `mcp-foo`):

1. **Decide the cost metric.** Per-char? Per-page? Per-call? Pick one unit and one rate env var (`FOO_CREDIT_COST_PER_UNIT`).
2. **Add the deduct helper.** Copy `_deduct_for_translate` from `mcp-linguist/api/app.py`, rename to `_deduct_for_foo`, change `upstream` to `"foo"`.
3. **Call it on every billed endpoint** before doing the work. Pass the `Authorization` header + the unit count.
4. **Add Railway env vars** on the new MCP: `OAUTH_INTROSPECT_URL`, `INTROSPECT_SECRET` (copy from `mcp-oauth-server`), `FOO_CREDIT_COST_PER_UNIT`.
5. **If a new app is integrating:** register a new `oauth_clients` row in Connect's Supabase (`is_public_client=true`), then follow Step 3 on the app side. Each integrating app gets its own client_id but shares the same Connect user pool.
6. **Test the 402 path.** Drain a test user's credits, hit the endpoint, confirm the upstream returns 402 and the app shows a top-up CTA.

Audit trail lands automatically in `oauth_usage_logs` (`endpoint = "introspect/foo"`, `credits_used`, `user_id`, `client_id`).

---

## Operational notes

- **Rotating `INTROSPECT_SECRET`:** update Connect first, then every upstream MCP in quick succession. There's no graceful overlap — old secret stops working the moment Connect rolls.
- **Rotating `OAUTH_LINK_ENCRYPTION_KEY`:** there's no migration path. All linked users must re-auth. Don't rotate unless compromised.
- **Refund on partial failure:** none built in. If translate succeeds but response delivery fails (e.g. client disconnect after deduction), the user pays. Acceptable for low per-call cost (<$0.01). For higher cost units, switch to "deduct after success" — call `/introspect` twice: once with `cost=null` to validate, then again with `cost>0` after the work succeeds.
- **Static token escape hatch:** `API_TOKENS` env on the upstream MCP lets service-to-service callers (internal jobs, the gateway) bypass per-user billing. Keep these tokens short and rotate them with `INTROSPECT_SECRET`.

---

## Reference PRs

| Repo | PR | What it adds |
|------|----|--------------|
| mcp-oauth-server | [#46](https://github.com/dsmoz/mcp-oauth-server/pull/46) | Cost-aware `/introspect` (deduct + log) |
| mcp-oauth-server | [#48](https://github.com/dsmoz/mcp-oauth-server/pull/48) | Public client pattern (single client_id, many users) |
| mcp-linguist | [#6](https://github.com/dsmoz/mcp-linguist/pull/6) | REST `/api` + per-char credit deduction |
| dsmoz-academia | [#66](https://github.com/dsmoz/dsmoz-academia/pull/66) | OAuth client + token storage + translate UI |

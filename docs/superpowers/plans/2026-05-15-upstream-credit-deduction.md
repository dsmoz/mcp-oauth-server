# Upstream Per-LLM-Call Credit Deduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deduct credits atomically from users when upstream MCP servers make LLM/API calls, using the existing `/introspect` endpoint with `cost` parameter.

**Architecture:** Gateway forwards the user's raw OAuth token as `X-User-Token` header to all upstream requests. Each upstream MCP extracts this token into a `current_user_token` ContextVar via its `BearerAuthMiddleware`. Central LLM functions call `deduct_credits()` before each LLM/API call, which POSTs to `/introspect` with `{token, cost, upstream}`. The introspect endpoint atomically deducts credits and returns `{"active": false}` if insufficient — upstream returns 402 to the caller.

**Tech Stack:** Python contextvars, httpx/requests, FastAPI/Starlette middleware, Supabase RPC `deduct_credits_user`, Railway env vars.

**Approved cost table:**

| MCP | credit_cost_per_call (flat fallback) | Per-LLM-call cost |
|-----|--------------------------------------|-------------------|
| kobotoolbox | 1 | n/a (no LLM) |
| microsoft365 | 1 | n/a (no LLM) |
| asset-manager | 1 | n/a (no LLM) |
| academia | 2 | 2 |
| surveylab | 2 | 2 |
| linguist | 3 | 3 |
| loom | 5 | 5 |
| dsmoz-nexus | 5 | 5 |
| design-engine | 5 | 5 |
| scholar | 3 | 3 |

---

## Phase 0 — New-user credit bonus ($5 on signup)

**Files:**
- Modify: `src/oauth/routes.py` — `register_submit` (line ~484) and DCR registration path (line ~506)
- Modify: `src/admin/routes.py` — `create_user_admin`

Currently `register_submit` sets `credit_balance=0.0`. Change to `5.0` everywhere a new user row is created.

- [ ] **Step 1: Update register_submit**

In `src/oauth/routes.py`, around line 484, find the dict passed to create the new user and change:
```python
# Before:
credit_balance=0.0,
# After:
credit_balance=5.0,
```

Also update the DCR registration path (around line 506) where a user row may be created with no credit balance.

- [ ] **Step 2: Update admin create_user**

In `src/admin/routes.py`, `create_user_admin`, confirm the default credit balance is also set to 5.0.

- [ ] **Step 3: Commit**

```bash
git add src/oauth/routes.py src/admin/routes.py
git commit -m "feat(users): new users start with 5.0 free credits on signup"
```

---

## Phase 0b — SQL migration (mcp_catalogue costs + credit_topup_requests table)

**Files:**
- Create: `migrations/0050_mcp_catalogue_credit_costs.sql`

- [ ] **Step 1: Write migration**

```sql
-- migrations/0050_mcp_catalogue_credit_costs.sql
UPDATE mcp_catalogue SET credit_cost_per_call = 1 WHERE slug = 'kobotoolbox';
UPDATE mcp_catalogue SET credit_cost_per_call = 1 WHERE slug = 'microsoft365';
UPDATE mcp_catalogue SET credit_cost_per_call = 1 WHERE slug = 'asset-manager';
UPDATE mcp_catalogue SET credit_cost_per_call = 2 WHERE slug = 'academia';
UPDATE mcp_catalogue SET credit_cost_per_call = 2 WHERE slug = 'surveylab';
UPDATE mcp_catalogue SET credit_cost_per_call = 3 WHERE slug = 'linguist';
UPDATE mcp_catalogue SET credit_cost_per_call = 5 WHERE slug = 'loom';
UPDATE mcp_catalogue SET credit_cost_per_call = 5 WHERE slug = 'dsmoz-nexus';
UPDATE mcp_catalogue SET credit_cost_per_call = 5 WHERE slug = 'design-engine';
UPDATE mcp_catalogue SET credit_cost_per_call = 3 WHERE slug = 'scholar';

-- Top-up request queue (no payment gateway — admin approves manually)
CREATE TABLE IF NOT EXISTS credit_topup_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES users(user_id),
    amount float NOT NULL,
    note text DEFAULT '',
    status text NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    created_at timestamptz DEFAULT now(),
    reviewed_at timestamptz,
    reviewed_by text
);
```

- [ ] **Step 2: Apply via Supabase MCP**

Use `mcp__plugin_supabase_supabase__execute_sql` with the migration SQL.

- [ ] **Step 3: Commit**

```bash
git add migrations/0050_mcp_catalogue_credit_costs.sql
git commit -m "feat(billing): set mcp_catalogue credit costs; add credit_topup_requests table"
```

---

## Phase 1 — Gateway: forward X-User-Token to all upstream requests

**Files:**
- Modify: `src/gateway/routes.py` (lines ~1140-1200, `_gateway_asgi` / invoke handlers)

The gateway currently sends `upstream_api_key` as bearer to upstreams. Upstreams need the user's real OAuth token to call `/introspect`. Pass it as `X-User-Token`.

- [ ] **Step 1: Find token extraction point in gateway routes**

In `src/gateway/routes.py`, `_get_bearer(request)` at line 1141 extracts the user's OAuth token. The `invoke_mcp_tool` and SSE/ASGI handlers use `extra_headers` dict passed to `_headers()`.

- [ ] **Step 2: Add X-User-Token to extra_headers**

In every call site in `src/gateway/routes.py` that builds `extra_headers` for upstream requests, add the user's token:

```python
# Find the token earlier in the handler (already done via _get_bearer or auth middleware)
# Then in the extra_headers dict:
extra_headers = {
    "X-User-Token": token,   # <-- add this line
    # ... existing per-MCP credential headers
}
```

The `_extra_headers_for(mcp, user_id)` function at line 189 builds per-user-MCP config headers. The token must be added at the call site (in `invoke_mcp_tool` handler and `_gateway_asgi`) where `token` is in scope.

Locate the invoke handler (search for `invoke_mcp_tool` route handler). It calls `_headers(api_key, user_id, client_id, extra_headers)`. Patch the extra_headers construction:

```python
extra_headers = _extra_headers_for(mcp, user_id)
extra_headers["X-User-Token"] = token  # pass user OAuth token for per-LLM-call billing
```

Do the same in the SSE/ASGI proxy path.

- [ ] **Step 3: Verify _headers() merges extra_headers**

Confirm `upstream.py` `_headers()` already does `headers.update(extra_headers)` — it does (lines 88-98). No change needed in `upstream.py`.

- [ ] **Step 4: Commit**

```bash
git add src/gateway/routes.py
git commit -m "feat(gateway): forward X-User-Token header to all upstream MCP requests"
```

---

## Phase 2 — Shared credit.py pattern (one per MCP server)

Each upstream MCP gets a `credit.py` (or `credits.py`) module with this exact pattern. The cost is configured via env var with a hard-coded default matching the approved table.

**Template `credit.py`** (copy-paste for each MCP, change `UPSTREAM_NAME` and default cost):

```python
import os
import requests
from contextvars import ContextVar

current_user_token: ContextVar[str | None] = ContextVar("current_user_token", default=None)

COST_PER_LLM_CALL: float = float(os.getenv("CREDIT_COST_PER_LLM_CALL", "3.0"))  # override per MCP


def deduct_credits(upstream_name: str) -> bool:
    """Deduct credits via /introspect. Returns True = proceed, False = reject (402)."""
    token = current_user_token.get()
    if not token:
        return True  # no user context (local/testing) — allow
    cost = COST_PER_LLM_CALL
    if cost <= 0:
        return True
    base_url = os.getenv("OAUTH_ISSUER_URL", "").rstrip("/")
    secret = os.getenv("INTROSPECT_SECRET", "").strip()
    if not base_url or not secret:
        return True  # not configured — fail open
    try:
        resp = requests.post(
            f"{base_url}/introspect",
            json={"token": token, "cost": cost, "upstream": upstream_name},
            headers={"x-introspect-secret": secret},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return True  # fail open on unexpected errors
        return resp.json().get("active", False)
    except Exception:
        return True  # fail open on network errors
```

**Returning 402 when deduct_credits returns False:**

In tool handler functions, after `deduct_credits()` returns False:

```python
# For FastAPI endpoints:
from fastapi import HTTPException
if not deduct_credits("mcp-linguist"):
    raise HTTPException(status_code=402, detail="Insufficient credits")

# For MCP tool handlers returning dicts:
if not deduct_credits("mcp-loom"):
    return {"error": "insufficient_credits", "message": "Insufficient credits to process this request"}
```

---

## Phase 3 — mcp-linguist

**Repo:** `/Users/danilodasilva/Documents/Programming/mcp-servers/mcp-linguist`

**Files:**
- Create: `mcp-linguist/tools/credit.py`
- Modify: `mcp-linguist/tools/context.py` — add `current_user_token` re-export
- Modify: `mcp-linguist/http_server.py` — extract X-User-Token in BearerAuthMiddleware
- Modify: `mcp-linguist/tools/translator.py` — add deduct_credits before each LLM call

- [ ] **Step 1: Create credit.py**

```python
# mcp-linguist/tools/credit.py
import os
import requests
from contextvars import ContextVar

current_user_token: ContextVar[str | None] = ContextVar("current_user_token", default=None)

COST_PER_LLM_CALL: float = float(os.getenv("CREDIT_COST_PER_LLM_CALL", "3.0"))


def deduct_credits(upstream_name: str = "mcp-linguist") -> bool:
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

- [ ] **Step 2: Extract X-User-Token in BearerAuthMiddleware (http_server.py)**

In `http_server.py`, `BearerAuthMiddleware.__call__()` already sets `current_client_id`. Add extraction of `X-User-Token` header and set `current_user_token`:

```python
from tools.credit import current_user_token

# Inside BearerAuthMiddleware.__call__, after setting current_client_id:
user_token = request.headers.get("x-user-token") or request.headers.get("X-User-Token")
token_ctx = current_user_token.set(user_token)
try:
    response = await call_next(request)
finally:
    current_user_token.reset(token_ctx)
```

- [ ] **Step 3: Add deduct_credits before each LLM call in translator.py**

`translator.py` has LLM calls at lines ~135, 186, 272, 409, 469, 538. Before each `_client().chat.completions.create(...)` call:

```python
from tools.credit import deduct_credits

# Before each chat.completions.create() call:
if not deduct_credits():
    raise HTTPException(status_code=402, detail="Insufficient credits")
```

- [ ] **Step 4: Run existing tests**

```bash
cd /Users/danilodasilva/Documents/Programming/mcp-servers/mcp-linguist
python -m pytest tests/ -v 2>&1 | head -50
```

- [ ] **Step 5: Commit**

```bash
git add tools/credit.py http_server.py tools/translator.py
git commit -m "feat(billing): per-LLM-call credit deduction via /introspect"
```

---

## Phase 4 — mcp-loom

**Repo:** `/Users/danilodasilva/Documents/Programming/mcp-servers/mcp-loom`

**Files:**
- Create: `mcp-loom/src/credits/credit.py`
- Modify: `mcp-loom/main.py` — add current_user_token ContextVar, extract in BearerAuthMiddleware
- Modify: `mcp-loom/src/llm/client.py` — add deduct_credits at top of `LLMClient.complete()`

- [ ] **Step 1: Create credit.py**

```python
# mcp-loom/src/credits/credit.py
import os
import requests
from contextvars import ContextVar

current_user_token: ContextVar[str | None] = ContextVar("current_user_token", default=None)

COST_PER_LLM_CALL: float = float(os.getenv("CREDIT_COST_PER_LLM_CALL", "5.0"))


def deduct_credits(upstream_name: str = "mcp-loom") -> bool:
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

- [ ] **Step 2: Add `__init__.py`**

```python
# mcp-loom/src/credits/__init__.py
```

- [ ] **Step 3: Extract X-User-Token in main.py BearerAuthMiddleware**

In `main.py` around line 196, `BearerAuthMiddleware` sets `current_client_id`. Add:

```python
from src.credits.credit import current_user_token

# Inside BearerAuthMiddleware.__call__, after setting current_client_id:
user_token = scope.get("headers_dict", {}).get(b"x-user-token", b"").decode() or None
# Or if using request object:
user_token = request.headers.get("x-user-token")
token_ctx = current_user_token.set(user_token)
try:
    await call_next(scope, receive, send)
finally:
    current_user_token.reset(token_ctx)
```

Read `main.py` first to confirm the exact middleware pattern (ASGI or Starlette style).

- [ ] **Step 4: Add deduct_credits in LLMClient.complete()**

In `src/llm/client.py`, at the top of `complete()` method (line ~72):

```python
from src.credits.credit import deduct_credits

class LLMClient:
    def complete(self, ...):
        if not deduct_credits():
            raise ValueError("Insufficient credits to process this LLM request")
        # existing code
        message = client.chat.completions.create(...)
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/danilodasilva/Documents/Programming/mcp-servers/mcp-loom
python -m pytest tests/ -v 2>&1 | head -50
```

- [ ] **Step 6: Commit**

```bash
git add src/credits/ main.py src/llm/client.py
git commit -m "feat(billing): per-LLM-call credit deduction via /introspect"
```

---

## Phase 5 — mcp-dsmoz-nexus

**Repo:** `/Users/danilodasilva/Documents/Programming/mcp-servers/mcp-dsmoz-nexus`

**Files:**
- Modify: `mcp-dsmoz-nexus/tools/context.py` — add `current_user_token` ContextVar
- Modify: `mcp-dsmoz-nexus/src/auth/middleware.py` — extract X-User-Token in BearerAuthMiddleware
- Create: `mcp-dsmoz-nexus/tools/credit.py`
- Modify: `mcp-dsmoz-nexus/tools/csos_profile.py` — add deduct_credits in `_translate_with_openrouter()`
- Modify: `mcp-dsmoz-nexus/vendor/kbase-core/kbase/chat/llm.py` — add deduct_credits in `stream_llm_response()` (if writable vendor copy)

- [ ] **Step 1: Create credit.py**

```python
# mcp-dsmoz-nexus/tools/credit.py
import os
import requests
from contextvars import ContextVar

current_user_token: ContextVar[str | None] = ContextVar("current_user_token", default=None)

COST_PER_LLM_CALL: float = float(os.getenv("CREDIT_COST_PER_LLM_CALL", "5.0"))


def deduct_credits(upstream_name: str = "mcp-dsmoz-nexus") -> bool:
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

- [ ] **Step 2: Add current_user_token to context.py**

In `tools/context.py`, after the existing `current_user_id` ContextVar:

```python
from tools.credit import current_user_token  # re-export for convenience
```

Or add directly:

```python
current_user_token: ContextVar[Optional[str]] = ContextVar("current_user_token", default=None)
```

- [ ] **Step 3: Extract X-User-Token in middleware.py**

In `src/auth/middleware.py`, `BearerAuthMiddleware.__call__()` around line 132. After reading headers:

```python
from tools.credit import current_user_token

# After reading x-user-id header:
raw_user_token = headers.get(b"x-user-token", b"").decode() or None
token_ctx = current_user_token.set(raw_user_token)
try:
    await self.app(scope, receive, send)
finally:
    current_user_token.reset(token_ctx)
```

- [ ] **Step 4: Add deduct_credits in csos_profile.py**

In `tools/csos_profile.py`, inside `_translate_with_openrouter()` before the HTTP POST to openrouter (line ~415):

```python
from tools.credit import deduct_credits

def _translate_with_openrouter(...):
    if not deduct_credits():
        raise RuntimeError("Insufficient credits")
    # existing httpx/requests call to openrouter
```

- [ ] **Step 5: Check vendor kbase llm.py**

Read `vendor/kbase-core/kbase/chat/llm.py`. If it's a local copy (not installed package), add deduct_credits at entry of `stream_llm_response()`. If installed package, only hook in `csos_profile.py`.

- [ ] **Step 6: Commit**

```bash
git add tools/credit.py tools/context.py src/auth/middleware.py tools/csos_profile.py
git commit -m "feat(billing): per-LLM-call credit deduction via /introspect"
```

---

## Phase 6 — mcp-design-engine

**Repo:** `/Users/danilodasilva/Documents/Programming/mcp-servers/mcp-design-engine`

**Files:**
- Create: `mcp-design-engine/src/credit.py`
- Modify: `mcp-design-engine/src/server.py` — extract X-User-Token in BearerAuthMiddleware
- Modify: `mcp-design-engine/tools/utils/files.py` — deduct before `_generate_tags_from_prompt()`
- Modify relevant generate tools — deduct before Recraft/ElevenLabs API calls

- [ ] **Step 1: Create credit.py**

```python
# mcp-design-engine/src/credit.py
import os
import requests
from contextvars import ContextVar

current_user_token: ContextVar[str | None] = ContextVar("current_user_token", default=None)

COST_PER_LLM_CALL: float = float(os.getenv("CREDIT_COST_PER_LLM_CALL", "5.0"))


def deduct_credits(upstream_name: str = "mcp-design-engine") -> bool:
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

- [ ] **Step 2: Extract X-User-Token in server.py BearerAuthMiddleware**

In `src/server.py` around line 29, `BearerAuthMiddleware` currently has no user context propagation. Add:

```python
from src.credit import current_user_token

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # existing auth logic ...
        user_token = request.headers.get("x-user-token")
        token_ctx = current_user_token.set(user_token)
        try:
            response = await call_next(request)
        finally:
            current_user_token.reset(token_ctx)
        return response
```

- [ ] **Step 3: Add deduct_credits in files.py and generate tools**

Read `tools/utils/files.py` to confirm `_generate_tags_from_prompt()` location. Add before openrouter call:

```python
from src.credit import deduct_credits

def _generate_tags_from_prompt(...):
    if not deduct_credits():
        raise HTTPException(status_code=402, detail="Insufficient credits")
    # existing openrouter httpx call
```

Read generate tool files (image/video/audio generation) and add `deduct_credits()` before each Recraft/ElevenLabs call.

- [ ] **Step 4: Commit**

```bash
git add src/credit.py src/server.py tools/
git commit -m "feat(billing): per-API-call credit deduction via /introspect"
```

---

## Phase 7 — mcp-scholar

**Repo:** `/Users/danilodasilva/Documents/Programming/mcp-servers/mcp-scholar`

**Files:**
- Create: `mcp-scholar/src/credit.py`
- Modify: middleware/server entry — extract X-User-Token
- Modify: `mcp-scholar/vendor/kbase/chat/llm.py` — add deduct_credits in `stream_llm_response()`
- Modify: `mcp-scholar/src/utils/llm_provider.py` — add deduct_credits for non-streaming calls
- Modify: `mcp-scholar/src/core/entity_extractor.py` — add deduct_credits

- [ ] **Step 1: Create credit.py**

```python
# mcp-scholar/src/credit.py
import os
import requests
from contextvars import ContextVar

current_user_token: ContextVar[str | None] = ContextVar("current_user_token", default=None)

COST_PER_LLM_CALL: float = float(os.getenv("CREDIT_COST_PER_LLM_CALL", "3.0"))


def deduct_credits(upstream_name: str = "mcp-scholar") -> bool:
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

- [ ] **Step 2: Find and modify middleware/server entry to extract X-User-Token**

Read `src/app.py` or `main.py` (whichever is the ASGI entry). Find `BearerAuthMiddleware` or equivalent. Add X-User-Token extraction — same pattern as other phases.

- [ ] **Step 3: Add deduct_credits in vendor/kbase/chat/llm.py**

At entry of `stream_llm_response()`:

```python
from src.credit import deduct_credits

def stream_llm_response(...):
    if not deduct_credits():
        yield '{"error": "insufficient_credits"}'
        return
    # existing streaming logic
```

- [ ] **Step 4: Add deduct_credits in llm_provider.py and entity_extractor.py**

Read each file. Add `deduct_credits()` before each LLM call. Return error dict if False.

- [ ] **Step 5: Commit**

```bash
git add src/credit.py src/ vendor/kbase/
git commit -m "feat(billing): per-LLM-call credit deduction via /introspect"
```

---

## Phase 8 — academia and surveylab (verify/implement)

**Repos:** `/Users/danilodasilva/Documents/Programming/mcp-servers/mcp-academia`, `/Users/danilodasilva/Documents/Programming/mcp-servers/mcp-surveylab`

Academia was confirmed to NOT already implement introspect calling. Both need the same pattern.

- [ ] **Step 1: For each MCP, read its existing middleware and LLM call sites**

Check for existing `credit.py` or `deduct_credits` usage. If absent, apply full pattern from Phase 2 template with costs academia=2, surveylab=2.

- [ ] **Step 2: Implement credit.py + middleware + LLM hook for each**

Follow same steps as Phases 3-7. Default `CREDIT_COST_PER_LLM_CALL=2.0` for both.

- [ ] **Step 3: Commit each**

```bash
git add . && git commit -m "feat(billing): per-LLM-call credit deduction via /introspect"
```

---

## Phase 9 — Railway environment variables

For each upstream MCP with credit deduction, set via Railway dashboard or CLI:

```
OAUTH_ISSUER_URL=https://<oauth-server-domain>
INTROSPECT_SECRET=<shared-secret-matching-oauth-server>
CREDIT_COST_PER_LLM_CALL=<per-MCP-value>
```

| MCP | CREDIT_COST_PER_LLM_CALL |
|-----|--------------------------|
| mcp-linguist | 3 |
| mcp-loom | 5 |
| mcp-dsmoz-nexus | 5 |
| mcp-design-engine | 5 |
| mcp-scholar | 3 |
| mcp-academia | 2 |
| mcp-surveylab | 2 |

- [ ] **Step 1: Verify INTROSPECT_SECRET set on oauth-server Railway service**

```bash
railway variables --service mcp-oauth-server | grep INTROSPECT_SECRET
```

- [ ] **Step 2: Set vars on each upstream service**

```bash
railway variables set OAUTH_ISSUER_URL=https://... INTROSPECT_SECRET=... CREDIT_COST_PER_LLM_CALL=3 --service mcp-linguist
# repeat for each MCP
```

---

## Phase 10 — Admin catalog: credit_cost_per_call field

**Context:** The `save_catalogue` form at `src/admin/routes.py:940` does not expose `credit_cost_per_call`. Admin must be able to set it per-MCP from the UI without a SQL migration every time.

**Files:**
- Modify: `src/admin/routes.py` — `save_catalogue` (line ~940), `edit_catalogue_form` (line ~928)
- Modify: `src/admin/templates/catalogue_form.html` — add numeric input for credit_cost_per_call

- [ ] **Step 1: Add field to save_catalogue handler**

In `src/admin/routes.py`, `save_catalogue` function. Add form parameter and update dict:

```python
async def save_catalogue(
    request: Request,
    slug: str,
    name: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    tier: str = Form("standard"),
    upstream_url: str = Form(...),
    upstream_api_key: str = Form(""),
    config_schema: str = Form(""),
    credit_cost_per_call: float = Form(0.0),   # <-- add this
    _: str = Depends(_require_admin),
):
    # In the update dict, add:
    update["credit_cost_per_call"] = credit_cost_per_call
```

- [ ] **Step 2: Pass current value in edit_catalogue_form**

In `edit_catalogue_form`, the `entry` dict is already passed from `_get_catalogue_row()` which selects `*` — so `entry["credit_cost_per_call"]` is available in the template context.

- [ ] **Step 3: Add input to catalogue_form.html**

In `src/admin/templates/catalogue_form.html`, add after the `tier` field:

```html
<div class="mb-3">
  <label class="form-label">Credit Cost Per Call</label>
  <input type="number" step="0.5" min="0" name="credit_cost_per_call"
         class="form-control" value="{{ entry.credit_cost_per_call or 0 }}">
  <div class="form-text">Credits deducted per gateway invoke_mcp_tool call (flat rate).</div>
</div>
```

- [ ] **Step 4: Commit**

```bash
git add src/admin/routes.py src/admin/templates/catalogue_form.html
git commit -m "feat(admin): editable credit_cost_per_call in catalogue form"
```

---

## Phase 11 — Credit top-up request flow (no payment gateway)

**Context:** No payment system exists. Users request a top-up via the portal; admin sees the queue and approves. A Telegram notification fires on each new request.

**Files:**
- Modify: `src/portal/routes.py` — replace `portal_credits_buy` with top-up request submission
- Modify: `src/portal/templates/portal_credits.html` — replace buy form with request form
- Modify: `src/admin/routes.py` — add topup request list and approve/reject handlers
- Create: `src/admin/templates/topup_requests.html`
- Modify: `src/telegram.py` — add `send_topup_request_notice()`
- Add link in `src/admin/templates/base.html` or `dashboard.html` to topup queue

- [ ] **Step 1: Add Telegram notification function**

In `src/telegram.py`, add:

```python
async def send_topup_request_notice(
    user_id: str,
    user_email: str,
    amount: float,
    note: str,
    request_id: str,
) -> None:
    """Notify admin of a new credit top-up request."""
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_OWNER_CHAT_ID:
        return
    text = (
        f"💳 *Credit Top-up Request*\n\n"
        f"User: `{user_email}` (`{user_id}`)\n"
        f"Amount: *{amount:.0f} credits*\n"
        f"Note: {note or '—'}\n\n"
        f"Review: /admin/topup-requests/{request_id}"
    )
    async with httpx.AsyncClient() as client:
        await client.post(
            _url("sendMessage"),
            json={"chat_id": settings.TELEGRAM_OWNER_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10.0,
        )
```

- [ ] **Step 2: Replace portal_credits_buy with submit_topup_request**

In `src/portal/routes.py`, replace the `portal_credits_buy` handler:

```python
@router.post("/credits/request", response_class=HTMLResponse)
async def portal_credits_request(
    request: Request,
    amount: float = Form(...),
    note: str = Form(""),
    user_id: str = Depends(_require_portal_user),
):
    if amount <= 0 or amount > 10000:
        raise HTTPException(status_code=400, detail="Invalid amount")
    user = _users().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Not found")
    db = get_db()
    result = db.table("credit_topup_requests").insert({
        "user_id": user_id,
        "amount": amount,
        "note": note.strip()[:500],
        "status": "pending",
    }).execute()
    request_id = result.data[0]["id"] if result.data else "unknown"
    from src.telegram import send_topup_request_notice
    import asyncio
    asyncio.create_task(send_topup_request_notice(
        user_id=user_id,
        user_email=user.email,
        amount=amount,
        note=note,
        request_id=request_id,
    ))
    return templates.TemplateResponse(
        request=request, name="portal_credits.html", context={
            "user": user,
            "active_nav": "credits",
            "credit_balance": float(user.credit_balance or 0),
            "success": f"Top-up request for {amount:.0f} credits submitted. Admin will review shortly.",
        }
    )
```

Remove the old `portal_credits_buy` handler and `_CREDIT_PLANS` dict.

- [ ] **Step 3: Update portal_credits.html**

Replace the buy plans form with a simple request form:

```html
<h5>Request Credit Top-up</h5>
<p class="text-muted">No payment gateway — contact admin via Telegram or submit a request below.</p>
<form method="post" action="/credits/request">
  <div class="mb-3">
    <label class="form-label">Credits requested</label>
    <input type="number" name="amount" min="10" max="10000" step="10" class="form-control" required>
  </div>
  <div class="mb-3">
    <label class="form-label">Note (optional)</label>
    <input type="text" name="note" class="form-control" maxlength="500">
  </div>
  <button type="submit" class="btn btn-primary">Submit Request</button>
</form>
```

- [ ] **Step 4: Add admin topup queue routes**

In `src/admin/routes.py`, add:

```python
@router.get("/topup-requests", response_class=HTMLResponse)
async def list_topup_requests(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    rows = db.table("credit_topup_requests").select(
        "*, users(email, display_name)"
    ).order("created_at", desc=True).limit(100).execute().data or []
    return templates.TemplateResponse(
        request=request, name="topup_requests.html",
        context={"requests": rows}
    )


@router.post("/topup-requests/{request_id}/approve", response_class=HTMLResponse)
async def approve_topup(
    request_id: str,
    admin_id: str = Depends(_require_admin),
):
    db = get_db()
    row = db.table("credit_topup_requests").select("*").eq("id", request_id).eq("status", "pending").limit(1).execute().data
    if not row:
        raise HTTPException(status_code=404, detail="Request not found or already processed")
    row = row[0]
    from src.users import SupabaseUserProvider
    SupabaseUserProvider().add_credits(row["user_id"], row["amount"])
    db.table("credit_topup_requests").update({
        "status": "approved",
        "reviewed_at": "now()",
        "reviewed_by": admin_id,
    }).eq("id", request_id).execute()
    return RedirectResponse(url="/admin/topup-requests", status_code=303)


@router.post("/topup-requests/{request_id}/reject", response_class=HTMLResponse)
async def reject_topup(
    request_id: str,
    admin_id: str = Depends(_require_admin),
):
    db = get_db()
    db.table("credit_topup_requests").update({
        "status": "rejected",
        "reviewed_at": "now()",
        "reviewed_by": admin_id,
    }).eq("id", request_id).eq("status", "pending").execute()
    return RedirectResponse(url="/admin/topup-requests", status_code=303)
```

- [ ] **Step 5: Create topup_requests.html template**

Create `src/admin/templates/topup_requests.html`. Extend `base.html`. Table with columns: Date, User, Amount, Note, Status, Actions (approve/reject buttons for pending rows).

- [ ] **Step 6: Add nav link in admin base or dashboard**

In `src/admin/templates/dashboard.html` or the nav partial, add link to `/admin/topup-requests`.

- [ ] **Step 7: Commit**

```bash
git add src/telegram.py src/portal/routes.py src/portal/templates/portal_credits.html \
        src/admin/routes.py src/admin/templates/topup_requests.html
git commit -m "feat(billing): credit top-up request queue with Telegram notification"
```

---

## Phase 12 — Introspect token validation cache

**Context:** Each upstream LLM call hits `/introspect` which does a Supabase `access_tokens` table lookup + optional RPC. Under load, this adds latency on every LLM call. Caching the token→user_id validation (not the deduction) for 60s reduces DB hits while keeping credit deduction atomic.

**Implementation location:** `src/oauth/routes.py` — cache the token lookup inside `introspect()`, not the deduction.

**Files:**
- Modify: `src/oauth/routes.py` — add TTLCache for token → (user_id, client_id, expires_at)

- [ ] **Step 1: Add cachetools dependency**

Check if `cachetools` is already installed:

```bash
grep cachetools /Users/danilodasilva/Documents/Programming/mcp-oauth-server/requirements.txt
```

If absent, add it:

```bash
echo "cachetools>=5.3" >> requirements.txt
uv pip install cachetools
```

- [ ] **Step 2: Add token validation cache in oauth/routes.py**

At the top of the module (after imports), add:

```python
from cachetools import TTLCache
import threading

# Cache token → (user_id, client_id, expires_at) for 60s.
# Deduction is never cached — only the token validity lookup.
_TOKEN_CACHE: TTLCache = TTLCache(maxsize=2000, ttl=60)
_TOKEN_CACHE_LOCK = threading.Lock()
```

In the `introspect()` handler, replace the `provider.load_access_token(body.token)` call with a cached version:

```python
@router.post("/introspect")
async def introspect(body: IntrospectRequest, ...):
    # ... secret check ...

    # Cached token validation
    cache_key = body.token  # token is already an opaque random string
    with _TOKEN_CACHE_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)

    if cached is None:
        provider = _provider()
        at = provider.load_access_token(body.token)
        if at is not None and not at.is_revoked:
            with _TOKEN_CACHE_LOCK:
                _TOKEN_CACHE[cache_key] = {
                    "user_id": at.user_id,
                    "client_id": at.client_id,
                    "expires_at": at.expires_at,
                    "is_revoked": at.is_revoked,
                }
            cached = _TOKEN_CACHE.get(cache_key)
    
    if cached is None:
        return JSONResponse({"active": False})
    
    from src.crypto import now_unix
    if cached["expires_at"] and cached["expires_at"] < now_unix():
        with _TOKEN_CACHE_LOCK:
            _TOKEN_CACHE.pop(cache_key, None)
        return JSONResponse({"active": False})

    # Credit deduction — never cached, always atomic
    if body.cost is not None and body.cost > 0:
        status, new_balance = _deduct_or_reject(cached["user_id"], body.cost)
        # ... rest unchanged ...
```

Also invalidate cache on token revocation. Find the token revocation endpoint and add:

```python
with _TOKEN_CACHE_LOCK:
    _TOKEN_CACHE.pop(revoked_token_value, None)
```

- [ ] **Step 3: Test cache hit behaviour**

```bash
# Two rapid introspect calls — second should not hit Supabase access_tokens lookup.
# Verify via logging: add print() in the cache-miss branch during dev.
```

- [ ] **Step 4: Commit**

```bash
git add src/oauth/routes.py requirements.txt
git commit -m "perf(introspect): TTL cache for token validation (60s); deduction always atomic"
```

---

## Verification

- [ ] **End-to-end test (per MCP):**
  1. Get a valid OAuth access token for a test user with known credit balance.
  2. Call an LLM-backed tool via the gateway (e.g., a translation in mcp-linguist).
  3. Confirm credits decrease by `CREDIT_COST_PER_LLM_CALL` in Supabase `users.credit_balance`.
  4. Drain credits to 0, call again — confirm 402 response with `"Insufficient credits"`.

- [ ] **Gateway X-User-Token test:**
  1. Inspect upstream request logs — confirm `X-User-Token` header present.
  2. Confirm introspect call appears in `oauth_usage_logs` with correct `user_id` and `credits_used`.

- [ ] **Fail-open test:**
  1. Temporarily unset `INTROSPECT_SECRET` on one MCP.
  2. Confirm LLM calls still succeed (fail-open behavior).
  3. Restore env var.

- [ ] **New-user signup bonus test:**
  1. Register a new user via `/register`.
  2. Check Supabase `users` table — confirm `credit_balance = 5.0`.

- [ ] **Top-up request flow test:**
  1. Log in to portal → Credits page — confirm "Request top-up" form is shown (no buy buttons).
  2. Submit a request for 50 credits.
  3. Confirm row appears in `credit_topup_requests` with `status = pending`.
  4. Confirm Telegram notification fired (check bot chat).
  5. In admin `/admin/topup-requests`, click Approve.
  6. Confirm user's `credit_balance` increased by 50.

- [ ] **Admin catalogue cost field test:**
  1. Go to `/admin/catalogue`, edit an MCP entry.
  2. Set `credit_cost_per_call` to 3.0, save.
  3. Confirm `mcp_catalogue.credit_cost_per_call` updated in DB.

- [ ] **Introspect cache test:**
  1. Make two rapid tool calls via the gateway.
  2. Check oauth-server logs — second call should show cache hit (no Supabase token lookup).
  3. Confirm credits still deducted on both calls (deduction not cached).

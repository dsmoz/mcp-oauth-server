# Per-User MCP Credentials — Migration Guide & Audit

> **Canonical implementation:** `mcp-kobotoolbox` (worktree `gallant-aryabhata-ba1ab2`)
> **Gateway/auth Supabase project:** `bwbghsnnrszdcmwqzjwv`
> **mcp-scholar Supabase project:** `kiwiguicrpkmjpxrxfrm`

---

## 1. When to Use Per-User Credentials

Migrate an MCP to per-user credentials when **each DS-MOZ portal user authenticates with the external service using their own account**. The test: if two portal users would have different data, quotas, or permissions inside the external service — they need separate keys.

| Signal | Decision |
|---|---|
| Every user logs in to the external platform independently | **Migrate** |
| There is a single DS-MOZ organisation account on the external platform | **Keep server-wide** |
| The credential is infrastructure config (DB URL, cache TTL, transport) | **Always server-wide** |
| A credential can be scoped to a specific user on the external service | **Migrate** |

**Migrate now:** KoboToolbox (done), Microsoft 365 (each user's mailbox/OneDrive), Zoom (user-owned recordings).

**Keep server-wide:** Brevo (single sender account), BoldSign (org e-signature account), Cloudinary/R2 (shared storage), Qdrant (shared vector DB), Cerebellum DB, Bug Tracker DB.

---

## 2. Implementation Checklist

### Step 1 — Define the credentials schema in Supabase

In the `mcp_catalogue` row for the MCP (column `credentials_schema`), store a JSON object where each key is a credential field name and the value describes the field.

```json
{
  "kobo_api_key": {
    "type": "string",
    "label": "KoboToolbox API Key",
    "secret": true,
    "required": true,
    "hint": "Account Settings → Security → API Key"
  },
  "kobo_server_url": {
    "type": "string",
    "label": "Server URL",
    "default": "https://kf.kobotoolbox.org",
    "required": false
  }
}
```

Field descriptor keys:

| Key | Required | Notes |
|---|---|---|
| `type` | Yes | `"string"` (only type supported) |
| `label` | Yes | Human-readable label in portal form |
| `secret` | No | `true` → `<input type="password">`, blank submission preserves stored value |
| `required` | No | `true` → HTML `required` attribute + validation |
| `default` | No | Pre-fills the field if no stored value |
| `hint` | No | Help text rendered below the field |

Run the SQL update directly or via the Supabase dashboard:

```sql
UPDATE mcp_catalogue
SET credentials_schema = '{
  "your_field": {"type": "string", "label": "...", "secret": true, "required": true}
}'::jsonb
WHERE slug = 'your-mcp-slug';
```

### Step 2 — Create `src/context.py`

Add a ContextVar for each credential field. Use empty string as the default for required fields (the `run` tool will check and return a friendly error if empty).

```python
# src/context.py
from contextvars import ContextVar

current_your_api_key: ContextVar[str] = ContextVar("current_your_api_key", default="")
current_your_server_url: ContextVar[str] = ContextVar("current_your_server_url", default="")
current_client_id: ContextVar[str | None] = ContextVar("current_client_id", default=None)
```

### Step 3 — Update `BearerAuthMiddleware` in `src/server.py`

Inside the `if scope["type"] == "http":` block, after the existing auth check, add the credential decoding block:

```python
from src.context import current_client_id, current_your_api_key, current_your_server_url

client_id_val = headers.get(b"x-client-id", b"").decode() or None

mcp_creds: dict = {}
raw_creds = headers.get(b"x-mcp-credentials", b"").decode()
if raw_creds:
    try:
        mcp_creds = json.loads(base64.b64decode(raw_creds))
    except Exception:
        pass

ctx_tokens = [
    current_client_id.set(client_id_val),
    current_your_api_key.set(mcp_creds.get("your_field_name", "")),
    current_your_server_url.set(
        mcp_creds.get("your_server_field", "https://default.example.com")
    ),
]
try:
    await self.app(scope, receive, send)
finally:
    for var, tok in zip(
        [current_client_id, current_your_api_key, current_your_server_url],
        ctx_tokens,
    ):
        var.reset(tok)
return
```

Required imports at top of `server.py`: `import base64`, `import json`.

### Step 4 — Update `execute_code()` in `src/executor.py`

Pass credentials from ContextVars into the execution function and re-set them for the worker thread:

```python
def execute_code(code: str, api_key: str = "", server_url: str = "") -> ExecutionResult:
    from src.context import current_your_api_key, current_your_server_url
    token_key = current_your_api_key.set(api_key)
    token_url = current_your_server_url.set(server_url)
    try:
        return _execute(code)
    finally:
        current_your_api_key.reset(token_key)
        current_your_server_url.reset(token_url)
```

### Step 5 — Update the `run` tool in `src/server.py`

Read from ContextVars, validate, and pass to `execute_code`:

```python
@mcp.tool()
async def run(code: str) -> str:
    from src.context import current_client_id, current_your_api_key, current_your_server_url

    if current_client_id.get() is None:
        return "Error: Request must be routed through the DS-MOZ gateway"

    api_key = current_your_api_key.get()
    if not api_key:
        return "Error: API key not configured. Visit the DS-MOZ portal to add your credentials."

    server_url = current_your_server_url.get() or "https://default.example.com"
    ...
    fn = functools.partial(execute_code, code, api_key, server_url)
```

### Step 6 — Update tool functions to read from ContextVars

Replace any `os.getenv("YOUR_API_KEY")` calls inside `tools/*.py` with ContextVar reads:

```python
# Before (server-wide env var)
import os
api_key = os.getenv("YOUR_API_KEY", "")

# After (per-user ContextVar)
from src.context import current_your_api_key
api_key = current_your_api_key.get()
```

For the `api/client.py` singleton pattern, ensure the client is constructed with the per-request key rather than being cached at module level. Pass the key from the ContextVar when constructing or fetching the client.

### Step 7 — Remove credential env vars from Railway deployment

Once per-user credentials are active, remove user-specific env vars from the Railway service environment. Keep infrastructure-only vars (transport, port, auth tokens, Sentry DSN, cache settings).

In Railway dashboard → Service → Variables, delete the migrated fields (e.g. `KOBO_API_KEY`, `KOBO_SERVER_URL`).

---

## 3. Reference Implementation — mcp-kobotoolbox

| File | Role |
|---|---|
| `src/context.py` | Declares `current_kobo_api_key`, `current_kobo_server_url`, `current_client_id` |
| `src/server.py` → `BearerAuthMiddleware.__call__()` | Decodes `X-MCP-Credentials` header, sets ContextVars, resets in `finally` |
| `src/server.py` → `run()` | Reads ContextVars, validates key is present, calls `execute_code()` |
| `src/executor.py` → `execute_code()` | Re-sets ContextVars in the worker thread, runs user code |
| `tools/assets.py` et al. | Read `current_kobo_api_key.get()` / `current_kobo_server_url.get()` |

Middleware decode pattern (verbatim from worktree `gallant-aryabhata-ba1ab2`):

```python
raw_creds = headers.get(b"x-mcp-credentials", b"").decode()
if raw_creds:
    mcp_creds = json.loads(base64.b64decode(raw_creds))
ctx_tokens = [
    current_client_id.set(client_id_val),
    current_kobo_api_key.set(mcp_creds.get("kobo_api_key", "")),
    current_kobo_server_url.set(
        mcp_creds.get("kobo_server_url", "https://kf.kobotoolbox.org")
    ),
]
```

---

## 4. MCP Server Environment Variable Audit

### mcp-kobotoolbox

> **Status: MIGRATED** — reference implementation. `KOBO_API_KEY` and `KOBO_SERVER_URL` are now per-user.

| Variable | Type | Notes |
|---|---|---|
| `KOBO_API_KEY` | ~~user-specific~~ | Migrated to `client_mcp_credentials` |
| `KOBO_SERVER_URL` | ~~user-specific~~ | Migrated to `client_mcp_credentials` |
| `KOBO_CACHE_TTL` | server-wide | Infrastructure — same for all users |
| `KOBO_DEFAULT_LIMIT` | server-wide | Infrastructure — same for all users |
| `API_TOKENS` | server-wide | Gateway bearer auth |
| `TRANSPORT` | server-wide | `stdio`/`http` |
| `PORT` | server-wide | Railway-injected |

---

### mcp-zoom

> **Status: MIGRATED** — per-user Zoom credentials implemented. `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` are now per-user.

| Variable | Type | Notes |
|---|---|---|
| `ZOOM_ACCOUNT_ID` | ~~user-specific~~ | Migrated to `client_mcp_credentials` |
| `ZOOM_CLIENT_ID` | ~~user-specific~~ | Migrated to `client_mcp_credentials` |
| `ZOOM_CLIENT_SECRET` | ~~user-specific~~ | Migrated to `client_mcp_credentials` |
| `TOKEN_CACHE_PATH` | server-wide | Infrastructure — token cache file location |
| `LOG_LEVEL` | server-wide | Infrastructure |
| `TRANSPORT` | server-wide | `stdio`/`http` |
| `PORT` | server-wide | Railway-injected |
| `API_TOKENS` | server-wide | Gateway bearer auth |
| `SENTRY_DSN` | server-wide | Error tracking |

**Implementation files:**

| File | Role |
|---|---|
| `src/context.py` | Declares `current_zoom_account_id`, `current_zoom_client_id`, `current_zoom_client_secret`, `current_client_id` |
| `src/server.py` → `BearerAuthMiddleware.__call__()` | Decodes `X-MCP-Credentials`, sets ContextVars, resets in `finally` |
| `src/server.py` → `run()` | Validates Zoom creds present before executing code |
| `src/client/zoom_client.py` | Per-request client: reads ContextVars, maintains `_user_clients` dict keyed by `(account_id, client_id)` |
| `src/auth/oauth.py` | `in_memory: bool = False` param — disables token file cache when using per-user creds |
| `src/auth/token_cache.py` | `cache_path: Path | None` — `None` = in-memory mode |

**credentials_schema in `mcp_catalogue`:**
```json
{
  "zoom_account_id": {"type": "string", "label": "Account ID", "secret": false, "required": true, "hint": "Zoom Marketplace → Server-to-Server OAuth app → Account ID"},
  "zoom_client_id": {"type": "string", "label": "Client ID", "secret": false, "required": true},
  "zoom_client_secret": {"type": "string", "label": "Client Secret", "secret": true, "required": true}
}
```

---

### mcp-microsoft365

| Variable | Type | Notes |
|---|---|---|
| `AZURE_CLIENT_ID` | server-wide | Single Azure app registration for DS-MOZ org |
| `AZURE_TENANT_ID` | server-wide | Tenant is per org, not per user |
| `TOKEN_CACHE_PATH` | server-wide | Infrastructure — token file path |
| `TRANSPORT` | server-wide | Infrastructure |
| `PORT` | server-wide | Railway-injected |
| `API_TOKENS` | server-wide | Gateway bearer auth |
| `LOG_LEVEL` | server-wide | Infrastructure |
| `SENTRY_DSN` | server-wide | Error tracking |

**Note:** Microsoft 365 uses OAuth2 interactive flow (MSAL), not API keys. Per-user identity is handled by the stored MSAL token cache, not by per-user env vars. The Azure app registration is shared. Migration to `client_mcp_credentials` is not straightforward — would require per-user token serialisation rather than a simple API key field. Track separately.

---

### mcp-brevo

| Variable | Type | Notes |
|---|---|---|
| `BREVO_API_KEY` | server-wide | Single DS-MOZ sender account on Brevo |
| `BREVO_BASE_URL` | server-wide | API base URL |

**Migration candidate:** No. Brevo is used as the organisation's email/marketing platform, not as individual user accounts. One shared API key is appropriate.

---

### mcp-boldsign

> `.env.example` not found in repository. Inferred from CLAUDE.md description ("BoldSign e-signature integration") and common BoldSign API patterns.

| Variable | Type | Notes |
|---|---|---|
| `BOLDSIGN_API_KEY` | server-wide | Single DS-MOZ organisation account |

**Migration candidate:** No. BoldSign is used as the org's e-signature platform. Documents are sent on behalf of the organisation, not individual users.

---

### mcp-qualitative-research

> Not found as a standalone server under `/mcp-servers/`. May be archived or integrated into another server. No `.env.example` available to audit.

---

### mcp-dsmoz-nexus

| Variable | Type | Notes |
|---|---|---|
| `TRANSPORT` | server-wide | Infrastructure |
| `PORT` | server-wide | Railway-injected |
| `API_TOKENS` | server-wide | Gateway bearer auth |
| `SUPABASE_URL` | server-wide | Shared DS-MOZ database |
| `SUPABASE_KEY` | server-wide | Shared publishable key |
| `ENV` | server-wide | Infrastructure |
| `INVOICE_NUMBER_START` | server-wide | Infrastructure / config |
| `QDRANT_URL` | server-wide | Shared vector DB cluster |
| `QDRANT_API_KEY` | server-wide | Shared vector DB cluster |
| `FIRECRAWL_API_KEY` | server-wide | Shared web research API |
| `TAVILY_API_KEY` | server-wide | Shared web research API |
| `DEEPL_API_KEY` | server-wide | Shared translation API |
| `OPENROUTER_API_KEY` | server-wide | Shared LLM API |
| `LOCAL_LLM_URL` | server-wide | Infrastructure |
| `LOCAL_LLM_MODEL` | server-wide | Infrastructure |
| `SENTRY_DSN` | server-wide | Error tracking |

**Migration candidate:** No. All credentials are infrastructure or shared service accounts for DS-MOZ's own operations.

---

### mcp-scholar

| Variable | Type | Notes |
|---|---|---|
| `TRANSPORT` | server-wide | Infrastructure |
| `PORT` | server-wide | Railway-injected |
| `MULTI_TENANT_MODE` | server-wide | Infrastructure |
| `API_TOKENS` | server-wide | Gateway bearer auth |
| `DATA_DIR` | server-wide | Infrastructure |
| `FILE_CONVERTER_URL` | server-wide | Internal service URL |
| `FILE_CONVERTER_TOKEN` | server-wide | Internal service auth |
| `MARKITDOWN_ENABLED` | server-wide | Infrastructure |
| `SENTRY_DSN` | server-wide | Error tracking |
| `ZOTERO_API_KEY` | user-specific | Each user has their own Zotero account |
| `ZOTERO_USER_ID` | user-specific | Per-user Zotero library ID |
| `ZOTERO_LIBRARY_TYPE` | user-specific | `user`/`group` — per-user |
| `OPENROUTER_API_KEY` | server-wide | Shared LLM API |
| `OPENROUTER_MODEL` | server-wide | Infrastructure |
| `ANTHROPIC_API_KEY` | server-wide | Shared LLM fallback |
| `ANTHROPIC_MODEL` | server-wide | Infrastructure |
| `LLM_API_URL` | server-wide | Shared local LLM |
| `LLM_API_KEY` | server-wide | Shared local LLM |
| `LLM_MODEL` | server-wide | Infrastructure |
| `LLM_URL_PICTURE_DESCRIPTION` | server-wide | Infrastructure |
| `LLM_API_KEY_PICTURE_DESCRIPTION` | server-wide | Shared |
| `LLM_MODEL_PICTURE_DESCRIPTION` | server-wide | Infrastructure |
| `ENABLE_PICTURE_DESCRIPTION` | server-wide | Feature flag |
| `OPENROUTER_IMAGE_MODEL` | server-wide | Infrastructure |
| `PICTURE_DESCRIPTION_MODEL` | server-wide | Infrastructure |
| `PICTURE_DESCRIPTION_TIMEOUT` | server-wide | Infrastructure |
| `ENABLE_AUDIO_TRANSCRIPTION` | server-wide | Feature flag |
| `EMBEDDING_BASE_URL` | server-wide | Shared embedding service |
| `EMBEDDING_API_KEY` | server-wide | Shared embedding service |
| `EMBEDDING_MODEL` | server-wide | Infrastructure |
| `EMBEDDING_VECTOR_SIZE` | server-wide | Infrastructure |
| `CHUNK_SIZE` | server-wide | Infrastructure |
| `CHUNK_OVERLAP` | server-wide | Infrastructure |
| `QDRANT_SERVER` | server-wide | Shared vector DB |
| `QDRANT_PORT` | server-wide | Infrastructure |
| `QDRANT_API_KEY` | server-wide | Shared vector DB |
| `QDRANT_DEFAULT_COLLECTION` | server-wide | Infrastructure |
| `PDF_MAX_CHUNK_SIZE_MB` | server-wide | Infrastructure |
| `PDF_MAX_CHUNK_PAGES` | server-wide | Infrastructure |
| `PDF_OVERLAP_PAGES` | server-wide | Infrastructure |
| `NEO4J_URI` | server-wide | Shared graph DB |
| `NEO4J_USER` | server-wide | Shared graph DB |
| `NEO4J_PASSWORD` | server-wide | Shared graph DB |
| `NEO4J_DATABASE` | server-wide | Infrastructure |

**Status: MIGRATED** — `ZOTERO_API_KEY`, `ZOTERO_USER_ID`, `ZOTERO_LIBRARY_TYPE` moved to `client_mcp_credentials` for user `usr_93f07c15894b4877`. Variables removed from Railway service environment.

**Implementation:** `X-MCP-Credentials` decoded in `src/auth/middleware.py` → `BearerAuthMiddleware`. Zotero creds merged into `ClientInfo.provider_config`; `zotero_connection.py` already reads `provider_config` in multi-tenant mode — no changes needed there.

**Supabase RLS fix applied 2026-05-14:** `kg_entity_relations` table had RLS disabled. Fixed:
```sql
ALTER TABLE public.kg_entity_relations ENABLE ROW LEVEL SECURITY;
```
No policies added (service role key bypasses RLS; matches pattern of `kg_entities` and `kg_document_entities`). Migration recorded as `enable_rls_kg_entity_relations` in Supabase migration history.

---

### mcp-cerebellum

| Variable | Type | Notes |
|---|---|---|
| `DATABASE_URL` | server-wide | Shared Cerebellum Postgres |
| `DATABASE_POOL_SIZE` | server-wide | Infrastructure |
| `DATABASE_MAX_OVERFLOW` | server-wide | Infrastructure |
| `QDRANT_URL` | server-wide | Shared vector DB |
| `QDRANT_API_KEY` | server-wide | Shared vector DB |
| `QDRANT_COLLECTION` | server-wide | Shared collection |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD` / `REDIS_URL` | server-wide | Shared cache/queue |
| `LLM_API_URL` | server-wide | Shared LLM |
| `LLM_API_KEY` | server-wide | Shared LLM |
| `LLM_MODEL` | server-wide | Infrastructure |
| `LLM_TEMPERATURE` | server-wide | Infrastructure |
| `LLM_MAX_TOKENS` | server-wide | Infrastructure |
| `ENABLE_CONTEXTUAL_RETRIEVAL` | server-wide | Feature flag |
| `CONTEXTUAL_RETRIEVAL_TIMEOUT` | server-wide | Infrastructure |
| `CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS` | server-wide | Infrastructure |
| `EMBEDDING_BASE_URL` | server-wide | Shared embedding service |
| `EMBEDDING_API_KEY` | server-wide | Shared embedding service |
| `EMBEDDING_MODEL` | server-wide | Infrastructure |
| `EMBEDDING_DIMENSIONS` | server-wide | Infrastructure |
| `APP_NAME` / `APP_VERSION` / `APP_ENV` / `DEBUG` / `LOG_LEVEL` | server-wide | Infrastructure |
| `API_HOST` / `API_PORT` / `API_PREFIX` / `CORS_ORIGINS` | server-wide | Infrastructure |
| `SECRET_KEY` / `ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | server-wide | Internal auth |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | server-wide | Initial setup only |
| `ZOTERO_API_KEY` | user-specific | Per-user Zotero connector |
| `ZOTERO_LIBRARY_ID` | user-specific | Per-user Zotero library |
| `ZOTERO_LIBRARY_TYPE` | user-specific | Per-user |
| `MS365_CLIENT_ID` | server-wide | Shared Azure app registration |
| `MS365_CLIENT_SECRET` | server-wide | Shared Azure app |
| `MS365_TENANT_ID` | server-wide | Shared tenant |
| `GOOGLE_CLIENT_ID` | server-wide | Shared Google OAuth app |
| `GOOGLE_CLIENT_SECRET` | server-wide | Shared Google OAuth app |
| `GOOGLE_REDIRECT_URI` | server-wide | Infrastructure |
| `JOB_QUEUE_PATH` / `JOB_MAX_WORKERS` / `JOB_RETRY_ATTEMPTS` / `JOB_RETRY_DELAY` | server-wide | Infrastructure |
| `MAX_DOCUMENT_SIZE_MB` / `CHUNK_SIZE` / `CHUNK_OVERLAP` / `PDF_OCR_ENABLED` | server-wide | Infrastructure |
| `UPLOADS_DIR` / `TEMP_DIR` / `DEFAULT_STORAGE_MODE` | server-wide | Infrastructure |
| `ENABLE_SYSTEM_MONITOR` / `MONITOR_INTERVAL_SECONDS` / `MONITOR_CPU_THRESHOLD` / `MONITOR_MEMORY_THRESHOLD` | server-wide | Infrastructure |
| `ENABLE_PUBLIC_COLLECTIONS` / `ENABLE_WEBHOOKS` / `ENABLE_ACTIVITY_LOG` | server-wide | Feature flags |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` | server-wide | Shared graph DB |

**Migration candidate:** Partial — `ZOTERO_API_KEY`, `ZOTERO_LIBRARY_ID`, `ZOTERO_LIBRARY_TYPE` are user-specific connector credentials. However, Cerebellum has its own internal user model and connector system — the migration approach may differ from the gateway X-MCP-Credentials pattern.

---

### mcp-design-engine

| Variable | Type | Notes |
|---|---|---|
| `TRANSPORT` | server-wide | Infrastructure |
| `PORT` | server-wide | Railway-injected |
| `API_TOKENS` | server-wide | Gateway bearer auth |
| `SENTRY_DSN` | server-wide | Error tracking |
| `RECRAFT_API_KEY` | server-wide | Shared DS-MOZ Recraft account |
| `RECRAFT_MODEL` | server-wide | Infrastructure |
| `OPENROUTER_API_KEY` | server-wide | Shared LLM API |
| `NANO_BANANA_MODEL` | server-wide | Infrastructure |
| `METADATA_LLM_MODEL` | server-wide | Infrastructure |
| `ELEVENLABS_API_KEY` | server-wide | Shared TTS account |
| `DEFAULT_ASPECT_RATIO` | server-wide | Infrastructure |
| `DEFAULT_OUTPUT_DIR` | server-wide | Infrastructure |
| `REQUEST_TIMEOUT` | server-wide | Infrastructure |
| `CLOUDFLARE_R2_ACCESS_KEY_ID` | server-wide | Shared R2 bucket |
| `CLOUDFLARE_R2_SECRET_ACCESS_KEY` | server-wide | Shared R2 bucket |
| `CLOUDFLARE_R2_BUCKET` | server-wide | Shared R2 bucket |
| `CLOUDFLARE_R2_ENDPOINT` | server-wide | Shared R2 bucket |
| `CLOUDFLARE_R2_PUBLIC_URL_BASE` | server-wide | Shared CDN URL |

**Migration candidate:** No. All generative AI and storage APIs are organisational shared accounts.

---

### mcp-bug-tracker

| Variable | Type | Notes |
|---|---|---|
| `DATABASE_PATH` | server-wide | SQLite file path |
| `ENV` | server-wide | Infrastructure |

**Migration candidate:** No. Bug tracker is a shared internal tool with no per-user external service accounts.

---

### mcp-asset-manager (asset-manager)

| Variable | Type | Notes |
|---|---|---|
| `CLOUDINARY_CLOUD_NAME` | server-wide | Shared DS-MOZ Cloudinary account |
| `CLOUDINARY_API_KEY` | server-wide | Shared Cloudinary account |
| `CLOUDINARY_API_SECRET` | server-wide | Shared Cloudinary account |
| `OPENROUTER_API_KEY` | server-wide | Shared LLM API |
| `OPENROUTER_MODEL` | server-wide | Infrastructure |
| `CLOUDFLARE_ACCOUNT_ID` | server-wide | Shared Cloudflare account |
| `CLOUDFLARE_API_TOKEN` | server-wide | Shared Cloudflare token |
| `CLOUDFLARE_R2_ACCESS_KEY_ID` | server-wide | Shared R2 bucket |
| `CLOUDFLARE_R2_SECRET_ACCESS_KEY` | server-wide | Shared R2 bucket |
| `CLOUDFLARE_R2_BUCKET` | server-wide | Shared R2 bucket |
| `CLOUDFLARE_R2_ENDPOINT` | server-wide | Infrastructure |
| `CLOUDFLARE_R2_PUBLIC_URL_BASE` | server-wide | Shared CDN URL |
| `CLOUDFLARE_IMAGES_ACCOUNT_HASH` | server-wide | Shared Cloudflare Images |
| `CLOUDFLARE_IMAGES_DEFAULT_VARIANT` | server-wide | Infrastructure |
| `CLOUDFLARE_STREAM_CUSTOMER_CODE` | server-wide | Shared Cloudflare Stream |
| `TRANSPORT` | server-wide | Infrastructure |
| `PORT` | server-wide | Railway-injected |
| `API_TOKENS` | server-wide | Gateway bearer auth |
| `SENTRY_DSN` | server-wide | Error tracking |

**Migration candidate:** No. All storage and CDN credentials are organisational shared accounts.

---

## 5. Migration Priority Summary

| MCP | Candidate | User-Specific Variables | Priority |
|---|---|---|---|
| `mcp-kobotoolbox` | Done | `KOBO_API_KEY`, `KOBO_SERVER_URL` | **DONE** |
| `mcp-zoom` | Done | `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` | **DONE** |
| `mcp-scholar` | Done | `ZOTERO_API_KEY`, `ZOTERO_USER_ID`, `ZOTERO_LIBRARY_TYPE` | **DONE** |
| `mcp-cerebellum` | Partial | `ZOTERO_API_KEY`, `ZOTERO_LIBRARY_ID`, `ZOTERO_LIBRARY_TYPE` | Medium — Cerebellum has its own user model; may need different approach |
| `mcp-microsoft365` | Deferred | MSAL token (not env var) | Low — requires per-user token serialisation, not simple API key |
| `mcp-brevo` | No | — | Not applicable |
| `mcp-boldsign` | No | — | Not applicable |
| `mcp-dsmoz-nexus` | No | — | Not applicable |
| `mcp-design-engine` | No | — | Not applicable |
| `mcp-bug-tracker` | No | — | Not applicable |
| `mcp-asset-manager` | No | — | Not applicable |
| `mcp-qualitative-research` | Unknown | — | Server not found; audit when located |

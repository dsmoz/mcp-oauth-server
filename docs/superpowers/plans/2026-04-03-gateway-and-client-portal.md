# Gateway & Client Portal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single SSE gateway endpoint per client with progressive tool disclosure, a self-service client portal for toggling MCPs, and an admin catalogue for managing available MCP servers.

**Architecture:** A new `mcp_catalogue` Supabase table holds published MCP servers. Clients pick from it via `/portal/mcps` (stored in `allowed_mcp_resources[]`). The `/gateway/{client_id}` SSE endpoint validates the client's Bearer token, then exposes 4 meta-tools (`search_tools`, `list_mcps`, `list_tools`, `call_tool`) that proxy to upstream SSE servers. The client portal authenticates with **username + password** (set on first login via a one-time setup link sent in the approval email). `oauth_clients` gains `portal_username` and `portal_password_hash` columns. A `portal_setup_tokens` table holds the one-time 24h setup tokens.

**Tech Stack:** FastAPI, FastMCP (SSE), `mcp` Python library (upstream client), `itsdangerous` (signed cookies), Jinja2, Supabase, existing CSS token system + Phosphor icons.

---

## Task 1: DB migration — mcp_catalogue table

**Files:**
- No source files — Supabase migration only

- [ ] **Step 1: Apply mcp_catalogue migration**

Run via `mcp__plugin_supabase_supabase__apply_migration`, project_id `bwbghsnnrszdcmwqzjwv`, name `create_mcp_catalogue`:

```sql
CREATE TABLE public.mcp_catalogue (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  slug             text        UNIQUE NOT NULL,
  name             text        NOT NULL,
  description      text        NOT NULL,
  category         text        NOT NULL,
  upstream_url     text        NOT NULL,
  upstream_api_key text        NOT NULL DEFAULT '',
  is_published     boolean     NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 2: Apply portal auth migration**

Run via `mcp__plugin_supabase_supabase__apply_migration`, name `portal_auth_columns`:

```sql
ALTER TABLE public.oauth_clients
  ADD COLUMN IF NOT EXISTS portal_username      text,
  ADD COLUMN IF NOT EXISTS portal_password_hash text;

CREATE TABLE public.portal_setup_tokens (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   text        NOT NULL REFERENCES public.oauth_clients(client_id) ON DELETE CASCADE,
  token_hash  text        NOT NULL UNIQUE,
  expires_at  timestamptz NOT NULL,
  used_at     timestamptz
);

CREATE INDEX portal_setup_tokens_token_hash_idx ON public.portal_setup_tokens (token_hash);
```

- [ ] **Step 3: Verify tables exist**

Run via `mcp__plugin_supabase_supabase__execute_sql`:
```sql
SELECT column_name FROM information_schema.columns
WHERE table_name = 'oauth_clients' AND column_name IN ('portal_username','portal_password_hash');
```
Expected: 2 rows.

- [ ] **Step 4: Seed the Linguist entry**

```sql
INSERT INTO public.mcp_catalogue (slug, name, description, category, upstream_url, upstream_api_key, is_published)
VALUES (
  'linguist',
  'Linguist',
  'DeepL-powered translation with glossary and formality control. Supports 30+ languages.',
  'Writing',
  'https://mcp-linguist-production.up.railway.app/sse',
  '',
  true
);
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: mcp_catalogue + portal auth DB migrations, linguist seed"
```

---

## Task 2: Dependencies + config

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/config.py`

- [ ] **Step 1: Add `mcp` and `itsdangerous` to pyproject.toml**

In `pyproject.toml`, update dependencies to:

```toml
dependencies = [
    "fastapi>=0.110.0",
    "uvicorn>=0.27.0",
    "supabase>=2.0.0",
    "bcrypt>=4.0.0",
    "python-dotenv>=1.0.0",
    "jinja2>=3.1.0",
    "python-multipart>=0.0.9",
    "structlog>=24.0.0",
    "httpx>=0.27.0",
    "pydantic>=2.5.0",
    "pydantic-settings>=2.1.0",
    "mcp>=1.0.0",
    "itsdangerous>=2.1.0",
]
```

- [ ] **Step 2: Add SECRET_KEY to config.py**

In `src/config.py`, add after `TELEGRAM_OWNER_CHAT_ID`:

```python
SECRET_KEY: str = "change-me-portal-secret"
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml src/config.py
git commit -m "feat: add mcp and itsdangerous dependencies, SECRET_KEY config"
```

---

## Task 3: Admin catalogue routes + templates

**Files:**
- Modify: `src/admin/routes.py` (add catalogue routes at bottom)
- Create: `src/admin/templates/catalogue_list.html`
- Create: `src/admin/templates/catalogue_form.html`
- Modify: `src/admin/templates/base.html` (add Catalogue nav link)

- [ ] **Step 1: Add catalogue routes to src/admin/routes.py**

Append to the end of `src/admin/routes.py`:

```python
# ── MCP Catalogue ─────────────────────────────────────────────────────────────

def _get_catalogue_row(db, slug: str) -> dict | None:
    result = db.table("mcp_catalogue").select("*").eq("slug", slug).limit(1).execute()
    return result.data[0] if result.data else None


@router.get("/catalogue", response_class=HTMLResponse)
async def list_catalogue(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    entries = db.table("mcp_catalogue").select("*").order("name").execute().data or []
    return templates.TemplateResponse(
        request=request, name="catalogue_list.html", context={"entries": entries}
    )


@router.get("/catalogue/new", response_class=HTMLResponse)
async def new_catalogue_form(request: Request, _: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="catalogue_form.html", context={"entry": None, "error": None}
    )


@router.post("/catalogue", response_class=HTMLResponse)
async def create_catalogue(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    upstream_url: str = Form(...),
    upstream_api_key: str = Form(""),
    _: str = Depends(_require_admin),
):
    db = get_db()
    if _get_catalogue_row(db, slug):
        return templates.TemplateResponse(
            request=request, name="catalogue_form.html",
            context={"entry": None, "error": f"Slug '{slug}' already exists"}
        )
    db.table("mcp_catalogue").insert({
        "slug": slug, "name": name, "description": description,
        "category": category, "upstream_url": upstream_url,
        "upstream_api_key": upstream_api_key, "is_published": False,
    }).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.get("/catalogue/{slug}/edit", response_class=HTMLResponse)
async def edit_catalogue_form(request: Request, slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    entry = _get_catalogue_row(db, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse(
        request=request, name="catalogue_form.html", context={"entry": entry, "error": None}
    )


@router.post("/catalogue/{slug}/edit", response_class=HTMLResponse)
async def save_catalogue(
    slug: str,
    name: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    upstream_url: str = Form(...),
    upstream_api_key: str = Form(""),
    _: str = Depends(_require_admin),
):
    db = get_db()
    if _get_catalogue_row(db, slug) is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.table("mcp_catalogue").update({
        "name": name, "description": description, "category": category,
        "upstream_url": upstream_url, "upstream_api_key": upstream_api_key,
    }).eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.post("/catalogue/{slug}/publish", response_class=HTMLResponse)
async def toggle_publish(slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    entry = _get_catalogue_row(db, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.table("mcp_catalogue").update({"is_published": not entry["is_published"]}).eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.post("/catalogue/{slug}/delete", response_class=HTMLResponse)
async def delete_catalogue(slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    db.table("mcp_catalogue").delete().eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)
```

- [ ] **Step 2: Create catalogue_list.html**

Create `src/admin/templates/catalogue_list.html`:

```html
{% extends "base.html" %}
{% block title %}MCP Catalogue — DS-MOZ MCP OAuth{% endblock %}
{% block content %}
<div class="page-header">
  <h1>MCP Catalogue</h1>
  <a href="/admin/catalogue/new" class="btn btn-primary">
    <i class="ph-light ph-plus"></i> Add MCP
  </a>
</div>

{% if entries %}
<table>
  <thead>
    <tr>
      <th>Name</th>
      <th>Category</th>
      <th>Upstream URL</th>
      <th>Status</th>
      <th>Actions</th>
    </tr>
  </thead>
  <tbody>
    {% for e in entries %}
    <tr>
      <td>
        <div style="font-weight:700;color:var(--text-h)">{{ e.name }}</div>
        <div style="font-size:0.75rem;color:var(--text-muted)">{{ e.description[:60] }}…</div>
      </td>
      <td><span class="badge badge-active">{{ e.category }}</span></td>
      <td style="font-family:monospace;font-size:0.75rem;color:var(--text-muted)">{{ e.upstream_url }}</td>
      <td>
        {% if e.is_published %}
          <span class="badge badge-active">Published</span>
        {% else %}
          <span class="badge badge-inactive">Draft</span>
        {% endif %}
      </td>
      <td class="actions">
        <a href="/admin/catalogue/{{ e.slug }}/edit" class="btn btn-secondary">
          <i class="ph-light ph-pencil-simple"></i> Edit
        </a>
        <form method="post" action="/admin/catalogue/{{ e.slug }}/publish" style="display:inline">
          <button type="submit" class="btn btn-secondary">
            <i class="ph-light ph-{% if e.is_published %}eye-slash{% else %}eye{% endif %}"></i>
            {% if e.is_published %}Unpublish{% else %}Publish{% endif %}
          </button>
        </form>
        <form method="post" action="/admin/catalogue/{{ e.slug }}/delete" style="display:inline"
              onsubmit="return confirm('Delete {{ e.name }}?')">
          <button type="submit" class="btn btn-danger">
            <i class="ph-light ph-trash"></i>
          </button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<div class="card" style="text-align:center;color:var(--text-muted);padding:3rem">
  <p>No MCP entries yet. <a href="/admin/catalogue/new" style="color:var(--accent-vivid)">Add your first MCP</a>.</p>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 3: Create catalogue_form.html**

Create `src/admin/templates/catalogue_form.html`:

```html
{% extends "base.html" %}
{% block title %}{% if entry %}Edit{% else %}New{% endif %} MCP — DS-MOZ MCP OAuth{% endblock %}
{% block content %}
<div class="page-header">
  <h1>{% if entry %}Edit {{ entry.name }}{% else %}New MCP{% endif %}</h1>
  <a href="/admin/catalogue" class="btn btn-secondary">
    <i class="ph-light ph-arrow-left"></i> Catalogue
  </a>
</div>

{% if error %}
<div class="card" style="border-color:var(--danger-border);background:var(--danger-bg);margin-bottom:1rem">
  <p style="color:var(--danger-fg);font-size:0.875rem">{{ error }}</p>
</div>
{% endif %}

<div class="card">
  <form method="post" action="{% if entry %}/admin/catalogue/{{ entry.slug }}/edit{% else %}/admin/catalogue{% endif %}">
    <div class="form-group">
      <label class="form-label">Slug <small style="color:var(--text-muted)">(machine key, e.g. linguist — cannot be changed after creation)</small></label>
      <input type="text" name="slug" class="form-input" required
             value="{{ entry.slug if entry else '' }}"
             {% if entry %}readonly style="opacity:0.6"{% endif %}>
    </div>
    <div class="form-group">
      <label class="form-label">Display Name</label>
      <input type="text" name="name" class="form-input" required value="{{ entry.name if entry else '' }}">
    </div>
    <div class="form-group">
      <label class="form-label">Description</label>
      <textarea name="description" class="form-input" rows="3" required>{{ entry.description if entry else '' }}</textarea>
    </div>
    <div class="form-group">
      <label class="form-label">Category</label>
      <select name="category" class="form-input">
        {% for cat in ["Research", "Writing", "Data", "Design"] %}
        <option value="{{ cat }}" {% if entry and entry.category == cat %}selected{% endif %}>{{ cat }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="form-group">
      <label class="form-label">Upstream SSE URL</label>
      <input type="url" name="upstream_url" class="form-input" required
             value="{{ entry.upstream_url if entry else '' }}"
             placeholder="https://mcp-linguist-production.up.railway.app/sse">
    </div>
    <div class="form-group">
      <label class="form-label">API Key <small style="color:var(--text-muted)">(leave blank if upstream requires no auth)</small></label>
      <input type="text" name="upstream_api_key" class="form-input"
             value="{{ entry.upstream_api_key if entry else '' }}"
             placeholder="optional">
    </div>
    <button type="submit" class="btn btn-primary">
      <i class="ph-light ph-floppy-disk"></i> Save
    </button>
  </form>
</div>
{% endblock %}
```

- [ ] **Step 4: Add Catalogue link to base.html sidebar**

In `src/admin/templates/base.html`, after the Clients nav item, add:

```html
<a href="/admin/catalogue" class="nav-item {% if request.url.path.startswith('/admin/catalogue') %}active{% endif %}" title="Catalogue">
  <i class="ph-light ph-plugs"></i>
  <span class="nav-label">Catalogue</span>
</a>
```

- [ ] **Step 5: Verify in browser**

Visit `/admin/catalogue`. Confirm Linguist entry appears (seeded in Task 1). Confirm create/edit/publish/delete all work.

- [ ] **Step 6: Commit**

```bash
git add src/admin/routes.py src/admin/templates/catalogue_list.html src/admin/templates/catalogue_form.html src/admin/templates/base.html
git commit -m "feat: admin MCP catalogue — CRUD + publish toggle"
```

---

## Task 4: Portal auth + base layout

**Files:**
- Create: `src/portal/__init__.py`
- Create: `src/portal/routes.py`
- Create: `src/portal/templates/portal_login.html`
- Create: `src/portal/templates/portal_setup_password.html`
- Create: `src/portal/templates/portal_base.html`
- Modify: `src/email.py` (add setup link to approval email)
- Modify: `src/oauth/routes.py` (generate setup token on reg_approve)
- Modify: `src/admin/routes.py` (generate setup token on admin approve)
- Modify: `main.py`

- [ ] **Step 1: Create src/portal/__init__.py**

```python
```
(empty file)

- [ ] **Step 2: Create src/portal/routes.py with auth**

```python
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config import get_settings
from src.crypto import generate_token, hash_secret, verify_secret
from src.db import get_db

router = APIRouter(prefix="/portal")

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

_SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours
_COOKIE_NAME = "portal_session"
_SETUP_TOKEN_TTL_HOURS = 24


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().SECRET_KEY, salt="portal")


def _sign_session(client_id: str) -> str:
    return _serializer().dumps({"client_id": client_id})


def _verify_session(token: str) -> Optional[str]:
    try:
        data = _serializer().loads(token, max_age=_SESSION_MAX_AGE)
        return data["client_id"]
    except (BadSignature, SignatureExpired, KeyError):
        return None


def _require_portal_client(request: Request) -> str:
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/portal/login"})
    client_id = _verify_session(token)
    if not client_id:
        raise HTTPException(status_code=302, headers={"Location": "/portal/login"})
    return client_id


def _get_client(client_id: str) -> Optional[dict]:
    db = get_db()
    result = db.table("oauth_clients").select("*").eq("client_id", client_id).limit(1).execute()
    return result.data[0] if result.data else None


def _hash_setup_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_setup_token(client_id: str) -> str:
    """Generate a one-time setup token, store hash in DB, return raw token."""
    raw = generate_token(32)
    token_hash = _hash_setup_token(raw)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=_SETUP_TOKEN_TTL_HOURS)).isoformat()
    get_db().table("portal_setup_tokens").insert({
        "client_id": client_id,
        "token_hash": token_hash,
        "expires_at": expires_at,
    }).execute()
    return raw


def _redeem_setup_token(raw: str) -> Optional[str]:
    """Validate token, return client_id if valid and unused. Returns None if invalid."""
    token_hash = _hash_setup_token(raw)
    db = get_db()
    result = db.table("portal_setup_tokens").select("*").eq("token_hash", token_hash).limit(1).execute()
    if not result.data:
        return None
    row = result.data[0]
    if row.get("used_at"):
        return None
    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires_at:
        return None
    return row["client_id"]


def _consume_setup_token(raw: str) -> None:
    """Mark token as used."""
    token_hash = _hash_setup_token(raw)
    get_db().table("portal_setup_tokens").update({
        "used_at": datetime.now(timezone.utc).isoformat()
    }).eq("token_hash", token_hash).execute()


# ── Login ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def portal_login_get(request: Request):
    return templates.TemplateResponse(
        request=request, name="portal_login.html", context={"error": None}
    )


@router.post("/login", response_class=HTMLResponse)
async def portal_login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    db = get_db()
    result = db.table("oauth_clients").select("*").eq("portal_username", username).eq("is_active", True).limit(1).execute()
    client = result.data[0] if result.data else None

    if client is None:
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Invalid username or password"}, status_code=401,
        )
    if not client.get("portal_password_hash"):
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Account not yet set up. Please use the setup link from your approval email."},
            status_code=401,
        )
    if not verify_secret(password, client["portal_password_hash"]):
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Invalid username or password"}, status_code=401,
        )

    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(
        _COOKIE_NAME, _sign_session(client["client_id"]),
        httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE,
    )
    return response


# ── Setup password (first login) ──────────────────────────────────────────────

@router.get("/setup-password", response_class=HTMLResponse)
async def setup_password_get(request: Request, token: str = ""):
    client_id = _redeem_setup_token(token)
    if not client_id:
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Setup link is invalid or has expired. Contact your administrator."},
        )
    client = _get_client(client_id)
    return templates.TemplateResponse(
        request=request, name="portal_setup_password.html",
        context={"token": token, "username": client.get("portal_username", ""), "error": None},
    )


@router.post("/setup-password", response_class=HTMLResponse)
async def setup_password_post(
    request: Request,
    token: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    client_id = _redeem_setup_token(token)
    if not client_id:
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Setup link is invalid or has expired. Contact your administrator."},
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            request=request, name="portal_setup_password.html",
            context={"token": token, "username": username, "error": "Passwords do not match"},
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request=request, name="portal_setup_password.html",
            context={"token": token, "username": username, "error": "Password must be at least 8 characters"},
        )

    db = get_db()
    db.table("oauth_clients").update({
        "portal_username": username.strip(),
        "portal_password_hash": hash_secret(password),
    }).eq("client_id", client_id).execute()
    _consume_setup_token(token)

    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(
        _COOKIE_NAME, _sign_session(client_id),
        httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE,
    )
    return response


@router.post("/logout")
async def portal_logout():
    response = RedirectResponse(url="/portal/login", status_code=303)
    response.delete_cookie(_COOKIE_NAME)
    return response
```

- [ ] **Step 3: Create portal_login.html**

Create `src/portal/templates/portal_login.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Client Portal — DS-MOZ Intelligence</title>
<link rel="stylesheet" href="https://unpkg.com/@phosphor-icons/web@2.1.1/src/light/style.css">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root { --bg: #060E10; --accent: #E8500A; --accent-vivid: #FF5E00; --focus: #115E67; --danger-fg: #FF6B6B; }
  body { font-family: 'Avenir Next','Avenir','Segoe UI',Helvetica Neue,Arial,sans-serif; background: var(--bg); display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #fff; border-radius: 12px; padding: 1.75rem; max-width: 400px; width: 100%; box-shadow: 0 12px 48px rgba(0,0,0,0.6); }
  .brand { font-size: 0.65rem; font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent-vivid); margin-bottom: 1.25rem; }
  h1 { font-size: 1.2rem; font-weight: 700; color: #0A1C20; margin-bottom: 0.25rem; }
  .sub { font-size: 0.85rem; color: #5A8A90; margin-bottom: 1.5rem; }
  .form-group { margin-bottom: 1rem; }
  label { display: block; font-size: 0.75rem; font-weight: 600; color: #0A1C20; margin-bottom: 0.3rem; }
  input { width: 100%; padding: 0.55rem 0.75rem; border: 1px solid #D4E8EA; border-radius: 6px; background: #F0F7F8; font-size: 0.875rem; color: #0A1C20; outline: none; }
  input:focus { border-color: var(--focus); }
  .btn { width: 100%; padding: 0.65rem; background: var(--accent); color: #fff; border: none; border-radius: 6px; font-size: 0.9rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 0.4rem; margin-top: 0.5rem; }
  .error { background: #FFF0F0; border: 1px solid #FFB3B3; border-radius: 6px; padding: 0.6rem 0.75rem; font-size: 0.8rem; color: var(--danger-fg); margin-bottom: 1rem; }
</style>
</head>
<body>
<div class="card">
  <div class="brand">DS-MOZ Intelligence</div>
  <h1>Client Portal</h1>
  <p class="sub">Sign in to manage your MCPs and get your gateway config</p>
  {% if error %}
  <div class="error"><i class="ph-light ph-warning"></i> {{ error }}</div>
  {% endif %}
  <form method="post" action="/portal/login">
    <div class="form-group">
      <label>Username</label>
      <input type="text" name="username" required autocomplete="username" placeholder="your@email.com">
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" name="password" required autocomplete="current-password" placeholder="••••••••">
    </div>
    <button type="submit" class="btn">
      <i class="ph-light ph-sign-in"></i> Sign In
    </button>
  </form>
</div>
</body>
</html>
```

- [ ] **Step 4: Create portal_setup_password.html**

Create `src/portal/templates/portal_setup_password.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Set Up Your Account — DS-MOZ Intelligence</title>
<link rel="stylesheet" href="https://unpkg.com/@phosphor-icons/web@2.1.1/src/light/style.css">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root { --bg: #060E10; --accent: #E8500A; --accent-vivid: #FF5E00; --focus: #115E67; --danger-fg: #FF6B6B; }
  body { font-family: 'Avenir Next','Avenir','Segoe UI',Helvetica Neue,Arial,sans-serif; background: var(--bg); display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #fff; border-radius: 12px; padding: 1.75rem; max-width: 420px; width: 100%; box-shadow: 0 12px 48px rgba(0,0,0,0.6); }
  .brand { font-size: 0.65rem; font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent-vivid); margin-bottom: 1.25rem; }
  h1 { font-size: 1.2rem; font-weight: 700; color: #0A1C20; margin-bottom: 0.25rem; }
  .sub { font-size: 0.85rem; color: #5A8A90; margin-bottom: 1.5rem; line-height: 1.5; }
  .form-group { margin-bottom: 1rem; }
  label { display: block; font-size: 0.75rem; font-weight: 600; color: #0A1C20; margin-bottom: 0.3rem; }
  input { width: 100%; padding: 0.55rem 0.75rem; border: 1px solid #D4E8EA; border-radius: 6px; background: #F0F7F8; font-size: 0.875rem; color: #0A1C20; outline: none; }
  input:focus { border-color: var(--focus); }
  .btn { width: 100%; padding: 0.65rem; background: var(--accent); color: #fff; border: none; border-radius: 6px; font-size: 0.9rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 0.4rem; margin-top: 0.5rem; }
  .error { background: #FFF0F0; border: 1px solid #FFB3B3; border-radius: 6px; padding: 0.6rem 0.75rem; font-size: 0.8rem; color: var(--danger-fg); margin-bottom: 1rem; }
  .hint { font-size: 0.72rem; color: #91BCC1; margin-top: 0.25rem; }
</style>
</head>
<body>
<div class="card">
  <div class="brand">DS-MOZ Intelligence</div>
  <h1>Set Up Your Account</h1>
  <p class="sub">Choose a username and password to access the client portal. Your username defaults to your email — you can change it.</p>
  {% if error %}
  <div class="error"><i class="ph-light ph-warning"></i> {{ error }}</div>
  {% endif %}
  <form method="post" action="/portal/setup-password">
    <input type="hidden" name="token" value="{{ token }}">
    <div class="form-group">
      <label>Username</label>
      <input type="text" name="username" required value="{{ username }}" autocomplete="username">
      <p class="hint">This is what you'll use to sign in.</p>
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" name="password" required autocomplete="new-password" placeholder="Min. 8 characters">
    </div>
    <div class="form-group">
      <label>Confirm Password</label>
      <input type="password" name="password_confirm" required autocomplete="new-password" placeholder="Repeat password">
    </div>
    <button type="submit" class="btn">
      <i class="ph-light ph-check"></i> Create Account & Sign In
    </button>
  </form>
</div>
</body>
</html>
```

- [ ] **Step 5: Update approval email to include setup link**

In `src/email.py`, update `send_approval_email` signature and HTML to include the setup link:

Change the function signature to:
```python
async def send_approval_email(
    contact_name: str,
    contact_email: str,
    company_name: str,
    client_id: str,
    raw_secret: str,
    issuer_url: str,
    setup_token: str,
) -> None:
```

Add a `setup_url` variable before building the HTML:
```python
setup_url = f"{issuer_url}/portal/setup-password?token={setup_token}"
```

In `_APPROVAL_HTML`, add after the secret box section:

```html
  <div class="section">
    <h2>Access Your Client Portal</h2>
    <p>Set up your username and password to manage your MCPs and get your gateway config:</p>
    <a href="{setup_url}" style="display:inline-block;margin-top:0.75rem;padding:0.65rem 1.25rem;background:#E8500A;color:#fff;border-radius:6px;font-weight:700;font-size:0.875rem;text-decoration:none;">Set Up Portal Account →</a>
    <p style="font-size:0.75rem;color:#91BCC1;margin-top:0.5rem;">This link expires in 24 hours.</p>
  </div>
```

Pass `setup_url=setup_url` into the `.format()` call on `_APPROVAL_HTML`.

- [ ] **Step 6: Generate setup token on Telegram approval**

In `src/oauth/routes.py`, in the `reg_approve` block, after the `send_approval_email` call add:

```python
from src.portal.routes import create_setup_token as _create_setup_token
# Set portal_username to contact email
db.table("oauth_clients").update({
    "portal_username": reg["contact_email"]
}).eq("client_id", client_id).execute()
setup_token = _create_setup_token(client_id)
```

Then pass `setup_token=setup_token` to `send_approval_email(...)`.

- [ ] **Step 7: Generate setup token on admin panel approval**

In `src/admin/routes.py`, in `approve_registration`, after the `asyncio.create_task(em.send_approval_email(...))` block add:

```python
from src.portal.routes import create_setup_token as _create_setup_token
db.table("oauth_clients").update({
    "portal_username": reg["contact_email"]
}).eq("client_id", client_id).execute()
setup_token = _create_setup_token(client_id)
```

Update the `asyncio.create_task` call to pass `setup_token=setup_token`.

- [ ] **Step 8: Create portal_base.html**

Create `src/portal/templates/portal_base.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{% block title %}DS-MOZ Intelligence Portal{% endblock %}</title>
<link rel="stylesheet" href="https://unpkg.com/@phosphor-icons/web@2.1.1/src/light/style.css">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #060E10; --surface: #0A1C20; --elevated: #0D2A30; --border: #16464F; --focus: #115E67;
    --accent: #E8500A; --accent-vivid: #FF5E00; --accent-dim: #C4420A; --amber: #FFAE62;
    --text-h: #F0F7F8; --text-b: #D4E8EA; --text-ui: #91BCC1; --text-muted: #5A8A90; --text-dim: #2E5A60;
    --success-bg: #073D20; --success-fg: #3DD68C;
    --danger-bg: #3D0A0A; --danger-fg: #FF6B6B; --danger-border: #5A1010;
    --info-bg: #052830; --info-fg: #7BCFD8;
  }
  body { font-family: 'Avenir Next','Avenir','Segoe UI',Helvetica Neue,Arial,sans-serif; background: var(--bg); color: var(--text-b); min-height: 100vh; }
  .app-shell { display: flex; min-height: 100vh; }

  /* Sidebar */
  .sidebar { width: 220px; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; flex-shrink: 0; }
  .sidebar-brand { padding: 1.25rem 1.5rem 1rem; border-bottom: 1px solid var(--border); }
  .brand-full { font-size: 0.65rem; font-weight: 800; letter-spacing: 0.12em; color: var(--accent-vivid); text-transform: uppercase; display: block; }
  .brand-client { font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .brand-short { display: none; }
  .sidebar-nav { flex: 1; padding: 1rem 0; }
  .nav-item { display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 1.5rem; color: var(--text-ui); text-decoration: none; font-size: 0.82rem; transition: background 0.1s; }
  .nav-item:hover { background: var(--elevated); color: var(--text-h); }
  .nav-item.active { background: var(--elevated); color: var(--text-h); border-left: 2px solid var(--accent-vivid); padding-left: calc(1.5rem - 2px); }
  .nav-item i { font-size: 18px; flex-shrink: 0; }
  .sidebar-footer { padding: 1rem 0; border-top: 1px solid var(--border); }

  /* Main */
  .main-content { flex: 1; padding: 1.5rem 2rem; overflow-y: auto; }
  .page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; flex-wrap: wrap; gap: 0.75rem; }
  .page-header h1 { font-size: 1.4rem; font-weight: 700; color: var(--text-h); }

  /* Components */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem 1.5rem; margin-bottom: 1rem; }
  .card h2 { font-size: 1rem; font-weight: 700; color: var(--text-h); margin-bottom: 1rem; }
  .btn { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.5rem 1rem; border-radius: 6px; border: none; font-size: 0.82rem; font-weight: 600; cursor: pointer; text-decoration: none; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: var(--accent-dim); }
  .btn-secondary { background: transparent; border: 1px solid var(--border); color: var(--text-ui); }
  .btn-secondary:hover { background: var(--elevated); color: var(--text-h); }
  .badge { display: inline-block; padding: 0.2em 0.65em; border-radius: 999px; font-size: 0.7rem; font-weight: 700; }
  .badge-research { background: #052830; color: #7BCFD8; }
  .badge-writing { background: var(--success-bg); color: var(--success-fg); }
  .badge-data { background: #1A0C00; color: var(--amber); }
  .badge-design { background: #1A0A00; color: var(--accent-vivid); }
  .code-block { background: #060E10; border: 1px solid var(--border); border-radius: 6px; padding: 1rem; font-family: 'Courier New', monospace; font-size: 0.78rem; color: var(--amber); white-space: pre; overflow-x: auto; margin: 0.5rem 0; }

  /* Mobile */
  @media (max-width: 767px) {
    .sidebar { width: 40px; }
    .brand-full, .brand-client, .nav-label { display: none; }
    .brand-short { display: block; font-size: 0.6rem; font-weight: 800; color: var(--accent-vivid); text-align: center; padding: 0.5rem 0; }
    .nav-item { padding: 0.6rem 0; justify-content: center; }
    .main-content { padding: 1rem; }
  }
</style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar">
    <div class="sidebar-brand">
      <span class="brand-full">DS-MOZ INTELLIGENCE</span>
      <span class="brand-short">DM</span>
      <span class="brand-client">{{ client_name }}</span>
    </div>
    <nav class="sidebar-nav">
      <a href="/portal/" class="nav-item {% if request.url.path == '/portal/' %}active{% endif %}" title="Overview">
        <i class="ph-light ph-squares-four"></i>
        <span class="nav-label">Overview</span>
      </a>
      <a href="/portal/mcps" class="nav-item {% if request.url.path == '/portal/mcps' %}active{% endif %}" title="My MCPs">
        <i class="ph-light ph-plugs"></i>
        <span class="nav-label">My MCPs</span>
      </a>
      <a href="/portal/setup" class="nav-item {% if request.url.path == '/portal/setup' %}active{% endif %}" title="Setup Guide">
        <i class="ph-light ph-book-open"></i>
        <span class="nav-label">Setup Guide</span>
      </a>
    </nav>
    <div class="sidebar-footer">
      <form method="post" action="/portal/logout">
        <button type="submit" class="nav-item btn" style="width:100%;background:none;border:none;cursor:pointer;" title="Sign Out">
          <i class="ph-light ph-sign-out"></i>
          <span class="nav-label">Sign Out</span>
        </button>
      </form>
    </div>
  </aside>
  <main class="main-content">
    {% block content %}{% endblock %}
  </main>
</div>
</body>
</html>
```

- [ ] **Step 5: Register portal router in main.py**

In `main.py`, add after the existing router imports:

```python
from src.portal.routes import router as portal_router
```

And after `app.include_router(admin_router)`:

```python
app.include_router(portal_router)
```

- [ ] **Step 6: Verify login works**

Start the server locally or deploy, visit `/portal/login`, enter a valid client_id + client_secret. Confirm redirect to `/portal/` (which will 404 for now — that's fine). Confirm invalid credentials show error.

- [ ] **Step 7: Commit**

```bash
git add src/portal/__init__.py src/portal/routes.py src/portal/templates/portal_login.html src/portal/templates/portal_base.html main.py
git commit -m "feat: client portal — login/logout with signed session cookie"
```

---

## Task 5: Portal overview + MCP toggle pages

**Files:**
- Modify: `src/portal/routes.py` (add overview + mcps routes)
- Create: `src/portal/templates/portal_overview.html`
- Create: `src/portal/templates/portal_mcps.html`

- [ ] **Step 1: Add overview + mcps routes to src/portal/routes.py**

Append to `src/portal/routes.py`:

```python
import datetime as _dt


@router.get("/", response_class=HTMLResponse)
async def portal_overview(request: Request, client_id: str = _dep()):
    db = get_db()
    client = _get_client(client_id)
    if client is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    today_start = _dt.datetime.utcnow().strftime("%Y-%m-%dT00:00:00Z")
    month_start = _dt.datetime.utcnow().strftime("%Y-%m-01T00:00:00Z")
    usage_today = db.table("oauth_usage_logs").select("*", count="exact").eq("client_id", client_id).gte("called_at", today_start).execute().count or 0
    usage_month = db.table("oauth_usage_logs").select("*", count="exact").eq("client_id", client_id).gte("called_at", month_start).execute().count or 0
    usage_total = db.table("oauth_usage_logs").select("*", count="exact").eq("client_id", client_id).execute().count or 0

    settings = get_settings()
    gateway_url = f"{settings.OAUTH_ISSUER_URL}/gateway/{client_id}"

    return templates.TemplateResponse(
        request=request, name="portal_overview.html", context={
            "client_name": client["client_name"],
            "client_id": client_id,
            "usage_today": usage_today,
            "usage_month": usage_month,
            "usage_total": usage_total,
            "gateway_url": gateway_url,
        }
    )


@router.get("/mcps", response_class=HTMLResponse)
async def portal_mcps_get(request: Request, client_id: str = _dep()):
    db = get_db()
    client = _get_client(client_id)
    if client is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    catalogue = db.table("mcp_catalogue").select("*").eq("is_published", True).order("name").execute().data or []
    enabled = set(client.get("allowed_mcp_resources") or [])

    return templates.TemplateResponse(
        request=request, name="portal_mcps.html", context={
            "client_name": client["client_name"],
            "catalogue": catalogue,
            "enabled": enabled,
        }
    )


@router.post("/mcps", response_class=HTMLResponse)
async def portal_mcps_post(request: Request, client_id: str = _dep()):
    form = await request.form()
    selected = list(form.getlist("mcps"))
    db = get_db()
    # Validate — only allow published slugs
    published = {r["slug"] for r in (db.table("mcp_catalogue").select("slug").eq("is_published", True).execute().data or [])}
    selected = [s for s in selected if s in published]
    db.table("oauth_clients").update({"allowed_mcp_resources": selected}).eq("client_id", client_id).execute()
    return RedirectResponse(url="/portal/mcps", status_code=303)
```

Note: replace `_dep()` with `Depends(_require_portal_client)` — add this alias at the top of the append block:

```python
_dep = lambda: Depends(_require_portal_client)
```

Actually write it without the lambda — use `Depends(_require_portal_client)` directly in each function signature.

- [ ] **Step 2: Create portal_overview.html**

Create `src/portal/templates/portal_overview.html`:

```html
{% extends "portal_base.html" %}
{% block title %}Overview — DS-MOZ Intelligence Portal{% endblock %}
{% block content %}
<div class="page-header">
  <h1>Welcome, {{ client_name }}</h1>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem;margin-bottom:1.5rem">
  <div class="card" style="text-align:center">
    <div style="font-size:2rem;font-weight:800;color:var(--accent-vivid);line-height:1">{{ usage_today }}</div>
    <div style="font-size:0.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.08em;margin-top:0.25rem">Calls Today</div>
  </div>
  <div class="card" style="text-align:center">
    <div style="font-size:2rem;font-weight:800;color:var(--accent-vivid);line-height:1">{{ usage_month }}</div>
    <div style="font-size:0.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.08em;margin-top:0.25rem">This Month</div>
  </div>
  <div class="card" style="text-align:center">
    <div style="font-size:2rem;font-weight:800;color:var(--text-ui);line-height:1">{{ usage_total }}</div>
    <div style="font-size:0.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.08em;margin-top:0.25rem">All Time</div>
  </div>
</div>

<div class="card">
  <h2>Your Gateway URL</h2>
  <p style="font-size:0.82rem;color:var(--text-muted);margin-bottom:0.75rem">Connect Claude Desktop or ChatGPT to this single endpoint to access all your enabled MCPs.</p>
  <div class="code-block">{{ gateway_url }}</div>
  <p style="font-size:0.75rem;color:var(--text-dim);margin-top:0.5rem">Use your access token as the Bearer token. Visit <a href="/portal/setup" style="color:var(--accent-vivid)">Setup Guide</a> for full config.</p>
</div>
{% endblock %}
```

- [ ] **Step 3: Create portal_mcps.html**

Create `src/portal/templates/portal_mcps.html`:

```html
{% extends "portal_base.html" %}
{% block title %}My MCPs — DS-MOZ Intelligence Portal{% endblock %}
{% block content %}
<div class="page-header">
  <h1>My MCPs</h1>
</div>
<form method="post" action="/portal/mcps">
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1rem;margin-bottom:1.5rem">
    {% for mcp in catalogue %}
    {% set is_on = mcp.slug in enabled %}
    <label style="cursor:pointer">
      <div class="card" style="border-color:{% if is_on %}var(--accent-vivid){% else %}var(--border){% endif %};transition:border-color 0.15s">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:0.75rem">
          <div>
            <div style="font-weight:700;color:var(--text-h);font-size:0.9rem;margin-bottom:0.25rem">{{ mcp.name }}</div>
            <span class="badge badge-{{ mcp.category | lower }}">{{ mcp.category }}</span>
          </div>
          <input type="checkbox" name="mcps" value="{{ mcp.slug }}" {% if is_on %}checked{% endif %}
                 style="width:18px;height:18px;accent-color:var(--accent-vivid);flex-shrink:0;margin-top:0.1rem">
        </div>
        <p style="font-size:0.78rem;color:var(--text-muted);margin-top:0.6rem;line-height:1.5">{{ mcp.description }}</p>
      </div>
    </label>
    {% endfor %}
  </div>
  {% if not catalogue %}
  <div class="card" style="text-align:center;color:var(--text-muted);padding:3rem">
    <p>No MCPs published yet. Check back soon.</p>
  </div>
  {% endif %}
  <button type="submit" class="btn btn-primary">
    <i class="ph-light ph-floppy-disk"></i> Save Selection
  </button>
</form>
{% endblock %}
```

- [ ] **Step 4: Commit**

```bash
git add src/portal/routes.py src/portal/templates/portal_overview.html src/portal/templates/portal_mcps.html
git commit -m "feat: portal overview + MCP toggle page"
```

---

## Task 6: Portal setup guide page

**Files:**
- Modify: `src/portal/routes.py` (add setup routes)
- Create: `src/portal/templates/portal_setup.html`

- [ ] **Step 1: Add setup routes to src/portal/routes.py**

Append to `src/portal/routes.py`:

```python
from fastapi.responses import FileResponse, Response
import json as _json
import tempfile as _tempfile


@router.get("/setup", response_class=HTMLResponse)
async def portal_setup(request: Request, client_id: str = Depends(_require_portal_client)):
    db = get_db()
    client = _get_client(client_id)
    if client is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    enabled_slugs = client.get("allowed_mcp_resources") or []
    mcps = []
    if enabled_slugs:
        mcps = db.table("mcp_catalogue").select("*").in_("slug", enabled_slugs).eq("is_published", True).execute().data or []

    settings = get_settings()
    gateway_url = f"{settings.OAUTH_ISSUER_URL}/gateway/{client_id}"

    # Build claude_desktop_config snippet
    claude_config = {
        "mcpServers": {
            "dsmoz-intelligence": {
                "url": gateway_url,
                "transport": "sse",
            }
        }
    }

    # Build ChatGPT config block
    chatgpt_config = (
        f"Authorization URL : {settings.OAUTH_ISSUER_URL}/oauth/authorize\n"
        f"Token URL         : {settings.OAUTH_ISSUER_URL}/oauth/token\n"
        f"Client ID         : {client_id}\n"
        f"Client Secret     : (your client secret)\n"
        f"Scope             : mcp"
    )

    return templates.TemplateResponse(
        request=request, name="portal_setup.html", context={
            "client_name": client["client_name"],
            "client_id": client_id,
            "gateway_url": gateway_url,
            "mcps": mcps,
            "claude_config": _json.dumps(claude_config, indent=2),
            "chatgpt_config": chatgpt_config,
        }
    )


@router.get("/setup/download")
async def portal_setup_download(client_id: str = Depends(_require_portal_client)):
    db = get_db()
    client = _get_client(client_id)
    if client is None:
        raise HTTPException(status_code=404)
    settings = get_settings()
    gateway_url = f"{settings.OAUTH_ISSUER_URL}/gateway/{client_id}"
    config = {
        "mcpServers": {
            "dsmoz-intelligence": {
                "url": gateway_url,
                "transport": "sse",
            }
        }
    }
    content = _json.dumps(config, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=claude_desktop_config.json"},
    )
```

- [ ] **Step 2: Create portal_setup.html**

Create `src/portal/templates/portal_setup.html`:

```html
{% extends "portal_base.html" %}
{% block title %}Setup Guide — DS-MOZ Intelligence Portal{% endblock %}
{% block content %}
<div class="page-header">
  <h1>Setup Guide</h1>
</div>

<div class="card">
  <h2>Your Active MCPs</h2>
  {% if mcps %}
  <div style="display:flex;flex-wrap:wrap;gap:0.5rem;margin-bottom:0.5rem">
    {% for mcp in mcps %}
    <span class="badge badge-{{ mcp.category | lower }}">{{ mcp.name }}</span>
    {% endfor %}
  </div>
  {% else %}
  <p style="font-size:0.82rem;color:var(--text-muted)">No MCPs selected. <a href="/portal/mcps" style="color:var(--accent-vivid)">Enable some MCPs first</a>.</p>
  {% endif %}
</div>

<div class="card">
  <h2>Claude Desktop</h2>
  <p style="font-size:0.82rem;color:var(--text-muted);margin-bottom:0.75rem">
    Add this to your <code style="color:var(--amber)">claude_desktop_config.json</code>.
    Location: <strong>macOS</strong> ~/Library/Application Support/Claude/ &nbsp;·&nbsp; <strong>Windows</strong> %APPDATA%\Claude\
  </p>
  <div class="code-block">{{ claude_config }}</div>
  <div style="display:flex;gap:0.75rem;margin-top:0.75rem">
    <a href="/portal/setup/download" class="btn btn-primary">
      <i class="ph-light ph-download-simple"></i> Download config file
    </a>
  </div>
</div>

<div class="card">
  <h2>ChatGPT / Custom GPT</h2>
  <p style="font-size:0.82rem;color:var(--text-muted);margin-bottom:0.75rem">Use these values when adding an OAuth connection to your Custom GPT action.</p>
  <div class="code-block">{{ chatgpt_config }}</div>
  <p style="font-size:0.75rem;color:var(--text-dim);margin-top:0.5rem">Your client secret was shown once on approval. If you've lost it, contact your administrator to rotate it.</p>
</div>
{% endblock %}
```

- [ ] **Step 3: Commit**

```bash
git add src/portal/routes.py src/portal/templates/portal_setup.html
git commit -m "feat: portal setup guide — config display + download"
```

---

## Task 7: Upstream MCP client

**Files:**
- Create: `src/gateway/__init__.py`
- Create: `src/gateway/upstream.py`

This module connects to upstream SSE MCP servers to fetch their tool list and call individual tools.

- [ ] **Step 1: Create src/gateway/__init__.py**

```python
```
(empty)

- [ ] **Step 2: Create src/gateway/upstream.py**

```python
"""
Upstream MCP client — connects to deployed SSE MCP servers.
Used by the gateway to fetch tool metadata and proxy tool calls.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession
from mcp import types as mcp_types


async def fetch_tools(upstream_url: str, api_key: str = "") -> list[dict]:
    """
    Connect to an upstream SSE MCP server and return its tool list.
    Returns list of {"name": str, "description": str, "inputSchema": dict}.
    """
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    try:
        async with sse_client(upstream_url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
                    }
                    for t in result.tools
                ]
    except Exception as exc:
        return []  # Upstream unreachable — return empty list, don't crash gateway


async def call_upstream_tool(
    upstream_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    api_key: str = "",
) -> str:
    """
    Call a specific tool on an upstream SSE MCP server.
    Returns the tool result as a JSON string.
    Raises ValueError on upstream error.
    """
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    async with sse_client(upstream_url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)

    # Flatten content blocks to a single string
    parts = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(block.text)
        else:
            parts.append(str(block))
    return "\n".join(parts) if parts else json.dumps({"result": "ok"})
```

- [ ] **Step 3: Commit**

```bash
git add src/gateway/__init__.py src/gateway/upstream.py
git commit -m "feat: upstream MCP client — fetch tools + call tool via SSE"
```

---

## Task 8: Gateway SSE endpoint

**Files:**
- Create: `src/gateway/routes.py`
- Modify: `main.py`

- [ ] **Step 1: Create src/gateway/routes.py**

```python
"""
DS-MOZ Intelligence Gateway — single SSE MCP endpoint per client.
Exposes 4 meta-tools: search_tools, list_mcps, list_tools, call_tool.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from mcp.server.fastmcp import FastMCP

from src.crypto import now_unix
from src.db import get_db
from src.oauth.provider import SupabaseOAuthProvider
from src.gateway.upstream import call_upstream_tool, fetch_tools

router = APIRouter()


def _load_client_mcps(client_id: str) -> list[dict]:
    """Return published catalogue rows for the client's enabled MCPs."""
    db = get_db()
    client_row = db.table("oauth_clients").select("allowed_mcp_resources").eq("client_id", client_id).limit(1).execute()
    if not client_row.data:
        return []
    slugs = client_row.data[0].get("allowed_mcp_resources") or []
    if not slugs:
        return []
    result = db.table("mcp_catalogue").select("*").in_("slug", slugs).eq("is_published", True).execute()
    return result.data or []


def _validate_bearer(authorization: Optional[str]) -> str:
    """Validate Bearer token and return client_id. Raises 401 on failure."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    provider = SupabaseOAuthProvider()
    at = provider.load_access_token(token)
    if at is None or at.is_revoked:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    if at.expires_at and at.expires_at < now_unix():
        raise HTTPException(status_code=401, detail="Token expired")
    return at.client_id


@router.get("/gateway/{client_id}")
async def gateway(
    client_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    token_client_id = _validate_bearer(authorization)
    if token_client_id != client_id:
        raise HTTPException(status_code=403, detail="Token does not belong to this client")

    mcps = _load_client_mcps(client_id)

    # Build FastMCP instance with 4 meta-tools
    mcp = FastMCP(f"dsmoz-gateway-{client_id}")

    @mcp.tool()
    async def list_mcps() -> str:
        """List all MCP services enabled for this client."""
        return json.dumps([
            {"slug": m["slug"], "name": m["name"], "description": m["description"], "category": m["category"]}
            for m in mcps
        ])

    @mcp.tool()
    async def list_tools(mcp_slug: str) -> str:
        """List all tools available in a specific MCP service. Use list_mcps() first to get valid slugs."""
        target = next((m for m in mcps if m["slug"] == mcp_slug), None)
        if target is None:
            return json.dumps({"error": f"MCP '{mcp_slug}' not found or not enabled"})
        tools = await fetch_tools(target["upstream_url"], target.get("upstream_api_key", ""))
        return json.dumps(tools)

    @mcp.tool()
    async def search_tools(query: str) -> str:
        """Search for tools by keyword across all enabled MCP services. Returns matching tools with their MCP slug."""
        query_lower = query.lower()
        results = []
        for m in mcps:
            tools = await fetch_tools(m["upstream_url"], m.get("upstream_api_key", ""))
            for t in tools:
                if query_lower in t["name"].lower() or query_lower in t.get("description", "").lower():
                    results.append({"mcp": m["slug"], "tool": t["name"], "description": t.get("description", "")})
        return json.dumps(results)

    @mcp.tool()
    async def call_tool(mcp_slug: str, tool_name: str, arguments: dict) -> str:
        """Call a specific tool on a specific MCP service. Use search_tools() or list_tools() first to find valid tools."""
        target = next((m for m in mcps if m["slug"] == mcp_slug), None)
        if target is None:
            return json.dumps({"error": f"MCP '{mcp_slug}' not found or not enabled"})
        try:
            result = await call_upstream_tool(
                target["upstream_url"], tool_name, arguments, target.get("upstream_api_key", "")
            )
            # Log usage
            try:
                get_db().table("oauth_usage_logs").insert({"client_id": client_id}).execute()
            except Exception:
                pass
            return result
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # Run as SSE
    return await mcp.run_sse_async()
```

- [ ] **Step 2: Register gateway router in main.py**

In `main.py`, add after portal router import:

```python
from src.gateway.routes import router as gateway_router
```

And register:

```python
app.include_router(gateway_router)
```

- [ ] **Step 3: Commit**

```bash
git add src/gateway/routes.py main.py
git commit -m "feat: gateway SSE endpoint — 4 meta-tools, bearer token auth"
```

---

## Task 9: Deploy + end-to-end verification

**Files:**
- No source changes — deploy and verify

- [ ] **Step 1: Deploy to Railway**

```bash
railway up --detach
```

- [ ] **Step 2: Verify admin catalogue**

Visit `/admin/catalogue`. Confirm Linguist entry is listed and published.

- [ ] **Step 3: Verify portal login**

Visit `https://mcp-oauth-server-production.up.railway.app/portal/login`. Log in with a valid client_id + client_secret. Confirm redirect to overview with usage stats and gateway URL.

- [ ] **Step 4: Toggle MCPs in portal**

Visit `/portal/mcps`. Check Linguist. Click Save. Confirm Supabase `allowed_mcp_resources` updated.

- [ ] **Step 5: Verify setup page**

Visit `/portal/setup`. Confirm gateway URL shown, claude_desktop_config.json block present, download button works.

- [ ] **Step 6: Test gateway SSE**

Use Claude Desktop or `curl` with a valid access token:

```bash
curl -N "https://mcp-oauth-server-production.up.railway.app/gateway/YOUR_CLIENT_ID" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Accept: text/event-stream"
```

Confirm SSE stream starts. Confirm `list_mcps` tool appears in the tool list.

- [ ] **Step 7: Commit verification note**

```bash
git commit --allow-empty -m "chore: gateway + portal verified end-to-end"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Task |
|---|---|
| `mcp_catalogue` DB table | Task 1 |
| `mcp` + `itsdangerous` deps | Task 2 |
| `SECRET_KEY` config | Task 2 |
| Admin catalogue CRUD + publish | Task 3 |
| Catalogue nav link in sidebar | Task 3 |
| Portal login with signed cookie | Task 4 |
| portal_base.html sidebar layout | Task 4 |
| Portal router registered in main.py | Task 4 |
| Portal overview with usage + gateway URL | Task 5 |
| MCP toggle page + POST save | Task 5 |
| Setup guide + download | Task 6 |
| Upstream SSE client (fetch_tools, call_upstream_tool) | Task 7 |
| Gateway route + 4 meta-tools | Task 8 |
| Gateway router registered in main.py | Task 8 |
| End-to-end deploy + verify | Task 9 |

All spec requirements covered. No placeholders found.

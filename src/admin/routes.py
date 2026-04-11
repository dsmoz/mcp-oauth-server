from __future__ import annotations

import datetime
import os as _os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from src.config import get_settings
from src.crypto import generate_client_id, generate_token, hash_secret, hash_token, now_unix
from src.db import get_db
from src import email as em
from src.oauth.provider import SupabaseOAuthProvider


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client_row(db, client_id: str) -> dict | None:
    """Safe single-row fetch — never uses maybe_single()."""
    result = db.table("oauth_clients").select("*").eq("client_id", client_id).limit(1).execute()
    return result.data[0] if result.data else None


def _get_registration_row(db, request_id: str) -> dict | None:
    result = (
        db.table("oauth_registration_requests")
        .select("*")
        .eq("id", request_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


router = APIRouter(prefix="/admin")

_TEMPLATES_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# Register unix timestamp → human-readable date filter
templates.env.filters["unix_to_date"] = lambda ts: (
    datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC") if ts else "—"
)

security = HTTPBasic()


def _require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    settings = get_settings()
    ok_user = secrets.compare_digest(credentials.username, settings.ADMIN_USERNAME)
    ok_pass = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _: str = Depends(_require_admin)):
    db = get_db()

    total_clients = db.table("oauth_clients").select("*", count="exact").execute().count or 0
    active_clients = (
        db.table("oauth_clients").select("*", count="exact").eq("is_active", True).execute().count or 0
    )
    active_tokens = (
        db.table("oauth_access_tokens")
        .select("*", count="exact")
        .eq("is_revoked", False)
        .gt("expires_at", now_unix())
        .execute()
        .count or 0
    )
    pending_requests = (
        db.table("oauth_registration_requests")
        .select("*", count="exact")
        .eq("status", "pending")
        .execute()
        .count or 0
    )
    recent_result = (
        db.table("oauth_clients")
        .select("client_id,client_name,created_at,is_active")
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )

    today_start = datetime.datetime.utcnow().strftime("%Y-%m-%dT00:00:00Z")
    month_start = datetime.datetime.utcnow().strftime("%Y-%m-01T00:00:00Z")
    calls_today = (
        db.table("oauth_usage_logs").select("*", count="exact").gte("called_at", today_start).execute().count or 0
    )
    calls_month = (
        db.table("oauth_usage_logs").select("*", count="exact").gte("called_at", month_start).execute().count or 0
    )

    # Performance metrics — top endpoints by avg duration (last 30 days)
    try:
        thirty_days_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat() + "Z"
        perf_stats = db.rpc("usage_stats_by_endpoint", {"p_since": thirty_days_ago}).execute().data or []
    except Exception:
        perf_stats = []

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "total_clients": total_clients,
            "active_clients": active_clients,
            "revoked_clients": total_clients - active_clients,
            "active_tokens": active_tokens,
            "pending_requests": pending_requests,
            "recent_clients": recent_result.data or [],
            "calls_today": calls_today,
            "calls_month": calls_month,
            "perf_stats": perf_stats,
        },
    )


# ── Client list ───────────────────────────────────────────────────────────────

@router.get("/clients/", response_class=HTMLResponse)
async def list_clients(request: Request, status: str = "active", _: str = Depends(_require_admin)):
    db = get_db()
    query = db.table("oauth_clients").select("*").order("created_at", desc=True)
    if status == "active":
        query = query.eq("is_active", True)
    elif status == "inactive":
        query = query.eq("is_active", False)
    # "all" — no filter
    result = query.execute()
    clients = result.data or []

    # Attach total call count to each client
    for c in clients:
        c["usage_total"] = (
            db.table("oauth_usage_logs").select("*", count="exact")
            .eq("client_id", c["client_id"]).execute().count or 0
        )

    return templates.TemplateResponse(
        request=request,
        name="clients_list.html",
        context={"clients": clients, "status_filter": status},
    )


# ── Create client ─────────────────────────────────────────────────────────────

@router.get("/clients/new", response_class=HTMLResponse)
async def new_client_form(request: Request, _: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request,
        name="client_create.html",
        context={"error": None},
    )


@router.post("/clients", response_class=HTMLResponse)
async def create_client(
    request: Request,
    client_name: str = Form(...),
    redirect_uris_raw: str = Form(""),
    created_by: Optional[str] = Form(None),
    _: str = Depends(_require_admin),
):
    redirect_uris = [u.strip() for u in redirect_uris_raw.splitlines() if u.strip()]
    client_id = generate_client_id()
    raw_secret = generate_token(32)
    secret_hash = hash_secret(raw_secret)

    db = get_db()
    db.table("oauth_clients").insert(
        {
            "client_id": client_id,
            "client_secret_hash": secret_hash,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code"],
            "scope": "mcp",
            "allowed_mcp_resources": [],
            "created_by": created_by or None,
            "is_active": True,
        }
    ).execute()

    return RedirectResponse(
        url=f"/admin/clients/{client_id}?secret={raw_secret}",
        status_code=303,
    )


# ── Client detail ─────────────────────────────────────────────────────────────

@router.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(
    request: Request,
    client_id: str,
    secret: Optional[str] = None,
    access_token: Optional[str] = None,
    _: str = Depends(_require_admin),
):
    db = get_db()
    client = _get_client_row(db, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    today_start = datetime.datetime.utcnow().strftime("%Y-%m-%dT00:00:00Z")
    month_start = datetime.datetime.utcnow().strftime("%Y-%m-01T00:00:00Z")
    usage_today = (
        db.table("oauth_usage_logs").select("*", count="exact")
        .eq("client_id", client_id).gte("called_at", today_start).execute().count or 0
    )
    usage_month = (
        db.table("oauth_usage_logs").select("*", count="exact")
        .eq("client_id", client_id).gte("called_at", month_start).execute().count or 0
    )
    usage_total = (
        db.table("oauth_usage_logs").select("*", count="exact")
        .eq("client_id", client_id).execute().count or 0
    )

    # Per-client performance breakdown
    try:
        client_perf = db.rpc("usage_stats_for_client", {"p_client_id": client_id}).execute().data or []
    except Exception:
        client_perf = []

    return templates.TemplateResponse(
        request=request,
        name="client_detail.html",
        context={
            "client": client,
            "secret": secret,
            "access_token": access_token,
            "usage_today": usage_today,
            "usage_month": usage_month,
            "usage_total": usage_total,
            "client_perf": client_perf,
        },
    )


# ── Edit client ───────────────────────────────────────────────────────────────

@router.get("/clients/{client_id}/edit", response_class=HTMLResponse)
async def edit_client_form(
    request: Request,
    client_id: str,
    _: str = Depends(_require_admin),
):
    db = get_db()
    client = _get_client_row(db, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return templates.TemplateResponse(
        request=request,
        name="client_edit.html",
        context={"client": client},
    )


@router.post("/clients/{client_id}/edit", response_class=HTMLResponse)
async def edit_client(
    client_id: str,
    client_name: str = Form(...),
    redirect_uris_raw: str = Form(""),
    _: str = Depends(_require_admin),
):
    db = get_db()
    if _get_client_row(db, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    redirect_uris = [u.strip() for u in redirect_uris_raw.splitlines() if u.strip()]
    db.table("oauth_clients").update(
        {"client_name": client_name, "redirect_uris": redirect_uris}
    ).eq("client_id", client_id).execute()
    return RedirectResponse(url=f"/admin/clients/{client_id}", status_code=303)


# ── Set portal credentials ───────────────────────────────────────────────────

@router.post("/clients/{client_id}/set-portal-credentials", response_class=HTMLResponse)
async def set_portal_credentials(
    client_id: str,
    portal_username: str = Form(...),
    portal_password: str = Form(""),
    _: str = Depends(_require_admin),
):
    db = get_db()
    if _get_client_row(db, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    update: dict = {"portal_username": portal_username or None}
    if portal_password:
        update["portal_password_hash"] = hash_secret(portal_password)
    db.table("oauth_clients").update(update).eq("client_id", client_id).execute()
    return RedirectResponse(url=f"/admin/clients/{client_id}", status_code=303)


# ── Add credits ───────────────────────────────────────────────────────────────

@router.post("/clients/{client_id}/add-credits", response_class=HTMLResponse)
async def add_credits(
    client_id: str,
    amount: float = Form(...),
    _: str = Depends(_require_admin),
):
    db = get_db()
    row = _get_client_row(db, client_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Client not found")
    current = float(row.get("credit_balance") or 0)
    db.table("oauth_clients").update({"credit_balance": current + amount}).eq("client_id", client_id).execute()
    return RedirectResponse(url=f"/admin/clients/{client_id}", status_code=303)


# ── Re-key client ─────────────────────────────────────────────────────────────

@router.post("/clients/{client_id}/rekey", response_class=HTMLResponse)
async def rekey_client(
    client_id: str,
    _: str = Depends(_require_admin),
):
    db = get_db()
    if _get_client_row(db, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    raw_secret = generate_token(32)
    secret_hash = hash_secret(raw_secret)
    db.table("oauth_clients").update({"client_secret_hash": secret_hash}).eq(
        "client_id", client_id
    ).execute()
    return RedirectResponse(
        url=f"/admin/clients/{client_id}?secret={raw_secret}",
        status_code=303,
    )


# ── Generate access token ─────────────────────────────────────────────────────

@router.post("/clients/{client_id}/generate-token", response_class=HTMLResponse)
async def generate_client_token(
    client_id: str,
    _: str = Depends(_require_admin),
):
    db = get_db()
    if _get_client_row(db, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    raw_token = generate_token(32)
    db.table("oauth_access_tokens").insert({
        "token": hash_token(raw_token),
        "client_id": client_id,
        "scopes": ["mcp"],
        "expires_at": 0,
        "is_revoked": False,
    }).execute()
    return RedirectResponse(
        url=f"/admin/clients/{client_id}?access_token={raw_token}",
        status_code=303,
    )


# ── Bulk delete clients ───────────────────────────────────────────────────────

@router.post("/clients/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_clients(
    request: Request,
    client_ids: list[str] = Form(default=[]),
    _: str = Depends(_require_admin),
):
    provider = SupabaseOAuthProvider()
    for client_id in client_ids:
        provider.delete_client(client_id)
    return RedirectResponse(url="/admin/clients/", status_code=303)


# ── Hard delete client ────────────────────────────────────────────────────────

@router.post("/clients/{client_id}/delete", response_class=HTMLResponse)
async def delete_client(
    client_id: str,
    _: str = Depends(_require_admin),
):
    db = get_db()
    if _get_client_row(db, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    provider = SupabaseOAuthProvider()
    provider.delete_client(client_id)
    return RedirectResponse(url="/admin/clients/", status_code=303)


# ── Revoke client (soft) ──────────────────────────────────────────────────────

@router.post("/clients/{client_id}/revoke", response_class=HTMLResponse)
async def revoke_client(
    client_id: str,
    _: str = Depends(_require_admin),
):
    db = get_db()
    db.table("oauth_clients").update({"is_active": False}).eq(
        "client_id", client_id
    ).execute()
    provider = SupabaseOAuthProvider()
    provider.revoke_client_tokens(client_id)
    from src.gateway.routes import evict_transport
    evict_transport(client_id)
    return RedirectResponse(url="/admin/clients/", status_code=303)


# ── Token inspector ───────────────────────────────────────────────────────────

@router.get("/clients/{client_id}/tokens", response_class=HTMLResponse)
async def client_tokens(
    request: Request,
    client_id: str,
    _: str = Depends(_require_admin),
):
    db = get_db()
    client = _get_client_row(db, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    result = (
        db.table("oauth_access_tokens")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .execute()
    )
    tokens = result.data or []
    # Annotate each token with computed state and display fingerprint
    ts_now = now_unix()
    for t in tokens:
        t["fingerprint"] = (t["token"] or "")[:12] + "…"
        if t.get("is_revoked"):
            t["state"] = "revoked"
        elif t.get("expires_at") and t["expires_at"] < ts_now:
            t["state"] = "expired"
        else:
            t["state"] = "active"
    return templates.TemplateResponse(
        request=request,
        name="client_tokens.html",
        context={"client": client, "tokens": tokens},
    )


@router.post("/clients/{client_id}/tokens/revoke", response_class=HTMLResponse)
async def revoke_token(
    client_id: str,
    token_hash: str = Form(...),
    _: str = Depends(_require_admin),
):
    db = get_db()
    db.table("oauth_access_tokens").update({"is_revoked": True}).eq(
        "token", token_hash
    ).execute()
    db.table("oauth_refresh_tokens").update({"is_revoked": True}).eq(
        "access_token", token_hash
    ).execute()
    return RedirectResponse(url=f"/admin/clients/{client_id}/tokens", status_code=303)


# ── Registration requests ─────────────────────────────────────────────────────

@router.get("/registrations", response_class=HTMLResponse)
async def list_registrations(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    result = (
        db.table("oauth_registration_requests")
        .select("*")
        .order("created_at", desc=True)
        .execute()
    )
    registrations = result.data or []
    return templates.TemplateResponse(
        request=request,
        name="registrations_list.html",
        context={"registrations": registrations},
    )


@router.get("/registrations/{request_id}", response_class=HTMLResponse)
async def registration_detail(
    request: Request,
    request_id: str,
    _: str = Depends(_require_admin),
):
    db = get_db()
    reg = _get_registration_row(db, request_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="Registration request not found")
    return templates.TemplateResponse(
        request=request,
        name="registration_detail.html",
        context={"reg": reg},
    )


@router.post("/registrations/{request_id}/approve", response_class=HTMLResponse)
async def approve_registration(
    request_id: str,
    admin: str = Depends(_require_admin),
):
    db = get_db()
    reg = _get_registration_row(db, request_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="Registration request not found")
    if reg["status"] != "pending":
        # Idempotent — already processed
        return RedirectResponse(url=f"/admin/registrations/{request_id}", status_code=303)

    # Generate client credentials
    client_id = generate_client_id()
    raw_secret = generate_token(32)
    secret_hash = hash_secret(raw_secret)
    redirect_uris = [
        u.strip() for u in (reg.get("redirect_uris_raw") or "").splitlines() if u.strip()
    ]

    db.table("oauth_clients").insert({
        "client_id": client_id,
        "client_secret_hash": secret_hash,
        "client_name": reg["company_name"],
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "scope": "mcp",
        "allowed_mcp_resources": [],
        "created_by": reg["contact_email"],
        "is_active": True,
        "portal_username": reg["contact_email"],
    }).execute()

    db.table("oauth_registration_requests").update({
        "status": "approved",
        "reviewed_at": "now()",
        "reviewed_by": admin,
    }).eq("id", request_id).execute()

    from src.portal.routes import create_setup_token
    setup_token = create_setup_token(client_id)

    import asyncio
    try:
        asyncio.create_task(em.send_approval_email(
            contact_name=reg.get("contact_name", reg["contact_email"]),
            contact_email=reg["contact_email"],
            company_name=reg["company_name"],
            client_id=client_id,
            raw_secret=raw_secret,
            issuer_url=get_settings().OAUTH_ISSUER_URL,
            setup_token=setup_token,
        ))
    except Exception as exc:
        import sys
        print(f"WARNING: approval email failed: {exc}", file=sys.stderr)

    return RedirectResponse(
        url=f"/admin/clients/{client_id}?secret={raw_secret}",
        status_code=303,
    )


@router.post("/registrations/{request_id}/reject", response_class=HTMLResponse)
async def reject_registration(
    request_id: str,
    admin: str = Depends(_require_admin),
):
    db = get_db()
    reg = _get_registration_row(db, request_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="Registration request not found")
    db.table("oauth_registration_requests").delete().eq("id", request_id).execute()
    return RedirectResponse(url="/admin/registrations", status_code=303)


# ── MCP Catalogue ─────────────────────────────────────────────────────────────

async def _auto_describe_mcp(upstream_url: str, api_key: str, name: str) -> str | None:
    """
    Fetch tool list from upstream MCP and use Claude to generate an AI-agent-friendly
    catalogue description. Falls back to a structured plain-text summary if Claude
    is unavailable or the upstream is unreachable.
    """
    try:
        from src.gateway.upstream import fetch_tool_list
        tools = await fetch_tool_list(upstream_url, api_key)
        if not tools:
            return None

        # Build a compact tool manifest to send to Claude
        tool_manifest = "\n".join(
            f"- {t['name']}: {t.get('description', '(no description)')}"
            for t in tools
        )

        settings = get_settings()
        if settings.ANTHROPIC_API_KEY:
            import httpx as _httpx
            prompt = (
                f"You are writing a catalogue entry for an MCP server called \"{name}\" "
                f"that will be read by an AI agent deciding which MCP to call.\n\n"
                f"Here are the tools it exposes:\n{tool_manifest}\n\n"
                f"Write a concise catalogue description (3-5 sentences) that:\n"
                f"1. States the server's primary purpose in one sentence\n"
                f"2. Lists the key capabilities an agent would care about\n"
                f"3. Gives clear guidance on WHEN to use this MCP vs others\n"
                f"4. Mentions that the agent should call list_tools or search_tools "
                f"with this MCP's slug to get the full tool list before calling any tool\n\n"
                f"Be specific and practical. Do not use bullet points — write flowing prose."
            )
            async with _httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": settings.ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 300,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    description = data["content"][0]["text"].strip()
                    if description:
                        return description

        # Fallback: structured plain-text summary
        tool_lines = "; ".join(
            f"{t['name']} ({t['description'][:80]})" if t.get("description") else t["name"]
            for t in tools[:10]
        )
        return (
            f"Provides tools for: {tool_lines}. "
            f"Use when the task requires any of these capabilities. "
            f"Call search_tools with a keyword or list_tools with this MCP's slug "
            f"to discover the full tool list before calling any tool."
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("_auto_describe_mcp failed for %s: %s", upstream_url, exc)
        return None


def _get_catalogue_row(db, slug: str) -> dict | None:
    result = db.table("mcp_catalogue").select("*").eq("slug", slug).limit(1).execute()
    return result.data[0] if result.data else None


@router.get("/catalogue", response_class=HTMLResponse)
async def list_catalogue(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    settings = get_settings()

    # Load existing catalogue rows keyed by slug
    db_rows: dict[str, dict] = {
        row["slug"]: row
        for row in (db.table("mcp_catalogue").select("*").execute().data or [])
    }

    # Fetch Railway services if configured
    railway_services: list[dict] = []
    railway_error: str | None = None
    if settings.RAILWAY_API_TOKEN and settings.RAILWAY_PROJECT_ID:
        try:
            from src.admin.railway import fetch_railway_services
            railway_services = await fetch_railway_services(
                settings.RAILWAY_API_TOKEN, settings.RAILWAY_PROJECT_ID,
                project_ids=settings.RAILWAY_PROJECT_IDS,
            )
        except Exception as exc:
            railway_error = str(exc)

    # Build merged list: each Railway service + any DB-only entries not in Railway
    railway_slugs = {svc["slug"] for svc in railway_services}

    entries = []
    for svc in railway_services:
        slug = svc["slug"]
        db_row = db_rows.get(slug)
        entries.append({
            "slug": slug,
            "name": svc["name"],
            "description": db_row["description"] if db_row else "",
            "category": db_row["category"] if db_row else "",
            "upstream_url": svc["upstream_url"] or (db_row["upstream_url"] if db_row else ""),
            "upstream_api_key": db_row["upstream_api_key"] if db_row else "",
            "is_published": db_row["is_published"] if db_row else False,
            "from_railway": True,
            "railway_id": svc["id"],
            "domain": svc["domain"],
        })

    # Include DB-only entries (manually added, not on Railway)
    for slug, row in db_rows.items():
        if slug not in railway_slugs:
            entries.append({**row, "from_railway": False, "railway_id": None, "domain": None})

    entries.sort(key=lambda e: e["name"].lower())

    return templates.TemplateResponse(
        request=request, name="catalogue_list.html",
        context={"entries": entries, "railway_error": railway_error,
                 "railway_configured": bool(settings.RAILWAY_API_TOKEN and settings.RAILWAY_PROJECT_ID)}
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
    request: Request,
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
async def toggle_publish(request: Request, slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    entry = _get_catalogue_row(db, slug)

    if entry is None:
        # Railway service not yet in DB — fetch its details and upsert
        settings = get_settings()
        if settings.RAILWAY_API_TOKEN and settings.RAILWAY_PROJECT_ID:
            from src.admin.railway import fetch_railway_services
            services = await fetch_railway_services(
                settings.RAILWAY_API_TOKEN, settings.RAILWAY_PROJECT_ID,
                project_ids=settings.RAILWAY_PROJECT_IDS,
            )
            svc = next((s for s in services if s["slug"] == slug), None)
            if svc is None:
                raise HTTPException(status_code=404, detail="Not found")
            upstream_url = svc["upstream_url"] or ""
            description = await _auto_describe_mcp(upstream_url, "", svc["name"]) or ""
            db.table("mcp_catalogue").insert({
                "slug": slug,
                "name": svc["name"],
                "description": description,
                "category": "MCP Server",
                "upstream_url": upstream_url,
                "upstream_api_key": "",
                "is_published": True,
            }).execute()
        else:
            raise HTTPException(status_code=404, detail="Not found")
    else:
        new_published = not entry["is_published"]
        update: dict = {"is_published": new_published}
        # Auto-refresh description when publishing (not unpublishing)
        if new_published:
            description = await _auto_describe_mcp(
                entry["upstream_url"], entry.get("upstream_api_key", ""), entry["name"]
            )
            if description:
                update["description"] = description
        db.table("mcp_catalogue").update(update).eq("slug", slug).execute()

    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.post("/catalogue/{slug}/refresh-description", response_class=HTMLResponse)
async def refresh_description(request: Request, slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    entry = _get_catalogue_row(db, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    description = await _auto_describe_mcp(
        entry["upstream_url"], entry.get("upstream_api_key", ""), entry["name"]
    )
    if description:
        db.table("mcp_catalogue").update({"description": description}).eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.post("/catalogue/{slug}/delete", response_class=HTMLResponse)
async def delete_catalogue(request: Request, slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    db.table("mcp_catalogue").delete().eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


# ── Settings ─────────────────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, _: str = Depends(_require_admin), saved: bool = False):
    from src.admin.settings import get_settings_grouped, CATEGORY_META
    grouped = get_settings_grouped()
    # Parse JSON options for select fields
    import json as _json
    for cat_settings in grouped.values():
        for s in cat_settings:
            if s.get("options") and isinstance(s["options"], str):
                try:
                    s["options"] = _json.loads(s["options"])
                except Exception:
                    s["options"] = []
    # Sort categories by display order
    sorted_cats = dict(sorted(CATEGORY_META.items(), key=lambda x: x[1]["order"]))
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"grouped": grouped, "categories": sorted_cats, "saved": saved, "error": None},
    )


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request, _: str = Depends(_require_admin)):
    from src.admin.settings import get_settings_by_category, set_setting
    form = await request.form()
    category = form.get("category", "")
    if not category:
        return RedirectResponse(url="/admin/settings", status_code=303)
    # Get all setting keys for this category
    cat_settings = get_settings_by_category(category)
    for s in cat_settings:
        key = s["key"]
        if key in form:
            new_value = form[key]
            # Don't overwrite secrets with empty values (means unchanged)
            if s["value_type"] == "secret" and not new_value:
                continue
            if new_value != (s["value"] or ""):
                set_setting(key, str(new_value))
    return RedirectResponse(url="/admin/settings?saved=true", status_code=303)

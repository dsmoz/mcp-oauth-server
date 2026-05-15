from __future__ import annotations

import datetime
import os as _os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from src.config import get_settings
from src.crypto import generate_client_id, generate_token, hash_secret, now_unix, verify_secret
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

security = HTTPBasic(auto_error=False)

_PORTAL_COOKIE = "portal_session"


def _verify_portal_session(token: str) -> Optional[str]:
    """Return user_id if the portal session cookie is valid, else None."""
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
    settings = get_settings()
    s = URLSafeTimedSerializer(settings.SECRET_KEY, salt="portal")
    try:
        data = s.loads(token, max_age=60 * 60 * 8)
        return data.get("user_id")
    except (BadSignature, SignatureExpired, KeyError):
        return None


def _require_admin(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> str:
    db = get_db()

    # ── Path 1: portal session cookie (admin users already logged in) ──────────
    cookie = request.cookies.get(_PORTAL_COOKIE)
    if cookie:
        user_id = _verify_portal_session(cookie)
        if user_id:
            result = (
                db.table("users")
                .select("email, is_admin")
                .eq("user_id", user_id)
                .eq("is_admin", True)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0]["email"]

    # ── Path 2: HTTP Basic Auth (direct / programmatic access) ─────────────────
    if credentials:
        result = (
            db.table("users")
            .select("user_id, email, password_hash, is_admin")
            .eq("email", credentials.username)
            .eq("is_admin", True)
            .limit(1)
            .execute()
        )
        user = result.data[0] if result.data else None
        if user and user.get("password_hash") and verify_secret(credentials.password, user["password_hash"]):
            return credentials.username

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _: str = Depends(_require_admin)):
    db = get_db()

    total_clients = db.table("oauth_clients").select("*", count="exact").execute().count or 0
    active_clients = (
        db.table("oauth_clients").select("*", count="exact").eq("is_active", True).execute().count or 0
    )
    total_users = db.table("users").select("*", count="exact").execute().count or 0
    active_users = (
        db.table("users").select("*", count="exact").eq("is_active", True).execute().count or 0
    )
    # Unclaimed DCR clients older than 24 hours — cleanup candidates
    cutoff_24h = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat() + "Z"
    unclaimed_stale = (
        db.table("oauth_clients")
        .select("*", count="exact")
        .is_("user_id", "null")
        .lt("created_at", cutoff_24h)
        .execute()
        .count or 0
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
            "total_users": total_users,
            "active_users": active_users,
            "unclaimed_stale": unclaimed_stale,
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


# ── Create public (multi-user) client ─────────────────────────────────────────

@router.post("/clients/public-create")
async def create_public_client(
    request: Request,
    _: str = Depends(_require_admin),
):
    """Provision a public OAuth client authorised by many distinct users.

    Public clients are intended for shared web applications (e.g. the
    dsmoz-academia portal) where a single ``client_id`` is consumed by many
    end users. The client is NOT bound to any single user — at consent time
    each authorising user gets tokens bound to *their* ``user_id``.

    Body (JSON):
        client_name (str): Human-readable name of the public application.
        redirect_uris (list[str]): Whitelisted redirect URIs.
        scope (str): Space-delimited scope string. Defaults to ``"mcp"``.

    Returns:
        ``{"client_id": ..., "client_secret": ..., "is_public_client": true}``
        — the secret is shown once; the caller must store it.

    Example:
        >>> # curl -u admin:pw -X POST https://oauth.example.com/admin/clients/public-create \
        ... #     -H 'Content-Type: application/json' \
        ... #     -d '{"client_name":"dsmoz-academia","redirect_uris":["https://academia.example.com/callback"]}'

    Raises:
        HTTPException 400: When ``client_name`` or ``redirect_uris`` is missing.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json_body")

    client_name = (body.get("client_name") or "").strip()
    redirect_uris = body.get("redirect_uris") or []
    scope = body.get("scope") or "mcp"

    if not client_name:
        raise HTTPException(status_code=400, detail="client_name is required")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris must be a non-empty list")

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
            "grant_types": ["authorization_code", "refresh_token"],
            "scope": scope if isinstance(scope, str) else " ".join(scope),
            "created_by": "admin:public",
            "is_active": True,
            "is_public_client": True,
            "user_id": None,
        }
    ).execute()

    from fastapi.responses import JSONResponse
    return JSONResponse(
        {
            "client_id": client_id,
            "client_secret": raw_secret,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "scope": scope,
            "is_public_client": True,
        },
        status_code=201,
    )


# ── Client detail ─────────────────────────────────────────────────────────────

@router.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(
    request: Request,
    client_id: str,
    secret: Optional[str] = None,
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

    # Create the user (tenant) first — reuse existing row if email already exists
    from src.users import SupabaseUserProvider
    users = SupabaseUserProvider()
    existing = users.get_user_by_email(reg["contact_email"])
    if existing:
        user = existing
    else:
        user = users.create_user(
            email=reg["contact_email"],
            display_name=reg["company_name"],
            is_active=False,  # flipped to True once password is set
        )

    # Generate per-device client credentials, bound to the user
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
        "user_id": user.user_id,
        "claimed_at": "now()",
    }).execute()

    db.table("oauth_registration_requests").update({
        "status": "approved",
        "reviewed_at": "now()",
        "reviewed_by": admin,
    }).eq("id", request_id).execute()

    from src.portal.routes import create_setup_token
    setup_token = create_setup_token(user.user_id)

    import asyncio
    try:
        asyncio.create_task(em.send_approval_email(
            contact_name=reg.get("contact_name", reg["contact_email"]),
            contact_email=reg["contact_email"],
            company_name=reg["company_name"],
            user_id=user.user_id,
            issuer_url=get_settings().OAUTH_ISSUER_URL,
            setup_token=setup_token,
        ))
    except Exception as exc:
        import sys
        print(f"WARNING: approval email failed: {exc}", file=sys.stderr)

    return RedirectResponse(
        url=f"/admin/users/{user.user_id}?secret={raw_secret}&client_id={client_id}",
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

async def _auto_describe_mcp(upstream_url: str, api_key: str, name: str) -> tuple[str | None, int | None]:
    """
    Fetch tool list from upstream MCP and use Claude to generate an AI-agent-friendly
    catalogue description. Falls back to a structured plain-text summary if Claude
    is unavailable or the upstream is unreachable.

    Returns (description, tool_count). Either may be None on failure.
    """
    try:
        from src.gateway.upstream import fetch_tool_list
        tools = await fetch_tool_list(upstream_url, api_key)
        if not tools:
            return None, None
        tool_count = len(tools)

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
                        return description, tool_count

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
        ), tool_count
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("_auto_describe_mcp failed for %s: %s", upstream_url, exc)
        return None, None


def _get_catalogue_row(db, slug: str) -> dict | None:
    result = db.table("mcp_catalogue").select("*").eq("slug", slug).limit(1).execute()
    return result.data[0] if result.data else None


async def _sync_tools_for_slug(slug: str, upstream_url: str, api_key: str) -> int:
    """Fetch tool list from upstream and upsert into mcp_tools. Returns tool count (0 on failure)."""
    try:
        from src.gateway.upstream import fetch_tool_list
        tools = await fetch_tool_list(upstream_url, api_key)
        if not tools:
            return 0
        db = get_db()
        rows = [
            {
                "mcp_slug": slug,
                "tool_name": t["name"],
                "description": t.get("description"),
                "input_schema": t.get("inputSchema"),
            }
            for t in tools
        ]
        db.table("mcp_tools").upsert(rows, on_conflict="mcp_slug,tool_name").execute()
        # Delete stale tools no longer exposed by the upstream
        current_names = [t["name"] for t in tools]
        db.table("mcp_tools").delete().eq("mcp_slug", slug).not_.in_("tool_name", current_names).execute()
        return len(tools)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("_sync_tools_for_slug failed for %s: %s", slug, exc)
        return 0


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
    if settings.RAILWAY_API_TOKEN:
        try:
            from src.admin.railway import fetch_railway_services
            railway_services = await fetch_railway_services(
                settings.RAILWAY_API_TOKEN,
                project_id=settings.RAILWAY_PROJECT_ID,
                project_ids=settings.RAILWAY_PROJECT_IDS,
            )
        except Exception as exc:
            railway_error = str(exc)

    # Build merged list: each Railway service + any DB-only entries not in Railway
    railway_slugs = {svc["slug"] for svc in railway_services}

    # Auto-publish new Railway MCP services (slug prefix "mcp-") that aren't in the DB yet
    for svc in railway_services:
        slug = svc["slug"]
        if slug in db_rows or not slug.startswith("mcp-"):
            continue
        upstream_url = svc["upstream_url"] or ""
        if not upstream_url:
            continue
        try:
            description, tool_count = await _auto_describe_mcp(upstream_url, "", svc["name"])
            inserted = db.table("mcp_catalogue").insert({
                "slug": slug,
                "name": svc["name"],
                "description": svc["name"],
                "description_agent": description or "",
                "tool_count": tool_count,
                "category": "MCP Server",
                "upstream_url": upstream_url,
                "upstream_api_key": "",
                "is_published": True,
            }).execute()
            if inserted.data:
                db_rows[slug] = inserted.data[0]
            await _sync_tools_for_slug(slug, upstream_url, "")
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("auto-publish failed for %s: %s", slug, exc)

    from urllib.parse import urlparse

    def _domain_from_url(url: str | None) -> str | None:
        if not url:
            return None
        try:
            host = urlparse(url).hostname
            return host or None
        except Exception:
            return None

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
            "is_featured": bool(db_row.get("is_featured")) if db_row else False,
            "icon": (db_row.get("icon") if db_row else None),
            "credit_cost_per_call": (db_row.get("credit_cost_per_call") if db_row else 0) or 0,
            "tool_count": (db_row.get("tool_count") if db_row else 0) or 0,
            "tier": (db_row.get("tier") if db_row else None) or "standard",
            "from_railway": True,
            "railway_id": svc["id"],
            "domain": svc["domain"],
        })

    # Include DB-only entries (manually added, not on Railway)
    for slug, row in db_rows.items():
        if slug not in railway_slugs:
            entries.append({
                **row,
                "tier": row.get("tier") or "standard",
                "from_railway": False,
                "railway_id": None,
                "domain": _domain_from_url(row.get("upstream_url")),
            })

    entries.sort(key=lambda e: e["name"].lower())

    return templates.TemplateResponse(
        request=request, name="catalogue_list.html",
        context={"entries": entries, "railway_error": railway_error,
                 "railway_configured": bool(settings.RAILWAY_API_TOKEN)}
    )


@router.get("/catalogue/new", response_class=HTMLResponse)
async def new_catalogue_form(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    return templates.TemplateResponse(
        request=request, name="catalogue_form.html",
        context={"entry": None, "error": None, "categories": _list_categories(db)},
    )


@router.post("/catalogue", response_class=HTMLResponse)
async def create_catalogue(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    description: str = Form(...),
    description_agent: str = Form(""),
    category: str = Form(...),
    tier: str = Form("standard"),
    upstream_url: str = Form(...),
    upstream_api_key: str = Form(""),
    icon: str = Form(""),
    is_featured: str = Form("off"),
    _: str = Depends(_require_admin),
):
    db = get_db()
    if _get_catalogue_row(db, slug):
        return templates.TemplateResponse(
            request=request, name="catalogue_form.html",
            context={"entry": None, "error": f"Slug '{slug}' already exists"}
        )
    tier_value = tier if tier in ("standard", "super") else "standard"
    db.table("mcp_catalogue").insert({
        "slug": slug, "name": name, "description": description,
        "description_agent": description_agent or description,
        "category": category, "tier": tier_value, "upstream_url": upstream_url,
        "upstream_api_key": upstream_api_key, "is_published": False,
        "icon": icon.strip() or None,
        "is_featured": is_featured == "on",
    }).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.get("/catalogue/{slug}/edit", response_class=HTMLResponse)
async def edit_catalogue_form(request: Request, slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    entry = _get_catalogue_row(db, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse(
        request=request, name="catalogue_form.html",
        context={"entry": entry, "error": None, "categories": _list_categories(db)},
    )


@router.post("/catalogue/{slug}/edit", response_class=HTMLResponse)
async def save_catalogue(
    request: Request,
    slug: str,
    name: str = Form(...),
    description: str = Form(...),
    description_agent: str = Form(""),
    category: str = Form(...),
    tier: str = Form("standard"),
    upstream_url: str = Form(...),
    upstream_api_key: str = Form(""),
    config_schema: str = Form(""),
    credit_cost_per_call: float = Form(0.0),
    icon: str = Form(""),
    is_featured: str = Form("off"),
    _: str = Depends(_require_admin),
):
    import json as _json
    db = get_db()
    if _get_catalogue_row(db, slug) is None:
        raise HTTPException(status_code=404, detail="Not found")
    update: dict = {
        "name": name, "description": description,
        "description_agent": description_agent or description,
        "category": category,
        "tier": tier if tier in ("standard", "super") else "standard",
        "upstream_url": upstream_url,
        "credit_cost_per_call": max(0.0, credit_cost_per_call),
        "icon": icon.strip() or None,
        "is_featured": is_featured == "on",
    }
    if upstream_api_key:
        update["upstream_api_key"] = upstream_api_key
    schema_str = config_schema.strip()
    if schema_str:
        try:
            parsed = _json.loads(schema_str)
            if not isinstance(parsed, list):
                raise ValueError("config_schema must be a JSON array")
            update["config_schema"] = parsed
        except (ValueError, _json.JSONDecodeError) as exc:
            entry = _get_catalogue_row(db, slug)
            return templates.TemplateResponse(
                request=request, name="catalogue_form.html",
                context={"entry": entry, "error": f"Invalid config_schema JSON: {exc}",
                         "categories": db.table("mcp_categories").select("*").order("name").execute().data or []},
                status_code=400,
            )
    else:
        update["config_schema"] = None
    db.table("mcp_catalogue").update(update).eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.post("/catalogue/{slug}/publish", response_class=HTMLResponse)
async def toggle_publish(request: Request, slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    entry = _get_catalogue_row(db, slug)

    if entry is None:
        # Railway service not yet in DB — fetch its details and upsert
        settings = get_settings()
        if settings.RAILWAY_API_TOKEN:
            from src.admin.railway import fetch_railway_services
            services = await fetch_railway_services(
                settings.RAILWAY_API_TOKEN,
                project_id=settings.RAILWAY_PROJECT_ID,
                project_ids=settings.RAILWAY_PROJECT_IDS,
            )
            svc = next((s for s in services if s["slug"] == slug), None)
            if svc is None:
                raise HTTPException(status_code=404, detail="Not found")
            upstream_url = svc["upstream_url"] or ""
            description, tool_count = await _auto_describe_mcp(upstream_url, "", svc["name"])
            db.table("mcp_catalogue").insert({
                "slug": slug,
                "name": svc["name"],
                "description": svc["name"],
                "description_agent": description or "",
                "tool_count": tool_count,
                "category": "MCP Server",
                "upstream_url": upstream_url,
                "upstream_api_key": "",
                "is_published": True,
            }).execute()
            await _sync_tools_for_slug(slug, upstream_url, "")
        else:
            raise HTTPException(status_code=404, detail="Not found")
    else:
        new_published = not entry["is_published"]
        update: dict = {"is_published": new_published}
        # Auto-refresh description when publishing (not unpublishing)
        if new_published:
            description, tool_count = await _auto_describe_mcp(
                entry["upstream_url"], entry.get("upstream_api_key", ""), entry["name"]
            )
            if description:
                update["description_agent"] = description
            if tool_count is not None:
                update["tool_count"] = tool_count
        db.table("mcp_catalogue").update(update).eq("slug", slug).execute()
        if new_published:
            await _sync_tools_for_slug(slug, entry["upstream_url"], entry.get("upstream_api_key", ""))

    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.post("/catalogue/{slug}/highlight", response_class=HTMLResponse)
async def toggle_highlight(slug: str, _: str = Depends(_require_admin)):
    """Toggle is_featured flag — controls visibility on landing-page carousel."""
    db = get_db()
    entry = _get_catalogue_row(db, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.table("mcp_catalogue").update({
        "is_featured": not bool(entry.get("is_featured")),
    }).eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.post("/catalogue/{slug}/generate-description")
async def generate_description(slug: str, _: str = Depends(_require_admin)):
    """Generate agent-facing description from upstream tools. Returns JSON — does NOT persist."""
    db = get_db()
    entry = _get_catalogue_row(db, slug)
    if entry is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    description, tool_count = await _auto_describe_mcp(
        entry["upstream_url"], entry.get("upstream_api_key", ""), entry["name"]
    )
    if not description:
        return JSONResponse({"error": "Failed to generate description"}, status_code=502)
    if tool_count is not None:
        db.table("mcp_catalogue").update({"tool_count": tool_count}).eq("slug", slug).execute()
    return JSONResponse({"description": description, "tool_count": tool_count})


@router.post("/catalogue/{slug}/refresh-description", response_class=HTMLResponse)
async def refresh_description(request: Request, slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    entry = _get_catalogue_row(db, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    description, tool_count = await _auto_describe_mcp(
        entry["upstream_url"], entry.get("upstream_api_key", ""), entry["name"]
    )
    update: dict = {}
    if description:
        update["description_agent"] = description
    if tool_count is not None:
        update["tool_count"] = tool_count
    if update:
        db.table("mcp_catalogue").update(update).eq("slug", slug).execute()
    await _sync_tools_for_slug(slug, entry["upstream_url"], entry.get("upstream_api_key", ""))
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.post("/catalogue/{slug}/sync-tools", response_class=HTMLResponse)
async def sync_tools(request: Request, slug: str, _: str = Depends(_require_admin)):
    """Sync tool list from upstream into mcp_tools table and update tool_count."""
    db = get_db()
    entry = _get_catalogue_row(db, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    count = await _sync_tools_for_slug(slug, entry["upstream_url"], entry.get("upstream_api_key", ""))
    if count:
        db.table("mcp_catalogue").update({"tool_count": count}).eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.post("/catalogue/{slug}/delete", response_class=HTMLResponse)
async def delete_catalogue(request: Request, slug: str, _: str = Depends(_require_admin)):
    db = get_db()
    db.table("mcp_catalogue").delete().eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


# ── Credit cost management ──────────────────────────────────────────────────

CREDIT_TIERS = [
    {"value": 0,  "label": "Free",         "desc": "0 credits — no cost"},
    {"value": 1,  "label": "Light",        "desc": "1 credit"},
    {"value": 2,  "label": "Standard",     "desc": "2 credits"},
    {"value": 3,  "label": "Premium",      "desc": "3 credits"},
    {"value": 5,  "label": "Heavy",        "desc": "5 credits"},
    {"value": 10, "label": "Super-heavy",  "desc": "10 credits"},
]


@router.post("/catalogue/{slug}/credits", response_class=HTMLResponse)
async def update_credit_cost(
    request: Request,
    slug: str,
    credit_cost_per_call: float = Form(0.0),
    _: str = Depends(_require_admin),
):
    """Inline update of credit_cost_per_call from catalogue list dropdown."""
    db = get_db()
    row = _get_catalogue_row(db, slug)
    if row is None:
        row = await _upsert_railway_slug(db, slug)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
    db.table("mcp_catalogue").update({
        "credit_cost_per_call": max(0.0, float(credit_cost_per_call)),
    }).eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


@router.get("/credits", response_class=HTMLResponse)
async def credits_manager(request: Request, _: str = Depends(_require_admin), saved: bool = False):
    """Bulk credit cost management page — all MCPs editable in one screen."""
    db = get_db()
    rows = (
        db.table("mcp_catalogue")
        .select("slug,name,category,tier,is_published,credit_cost_per_call")
        .order("name")
        .execute()
    ).data or []
    for r in rows:
        r["credit_cost_per_call"] = float(r.get("credit_cost_per_call") or 0)
    return templates.TemplateResponse(
        request=request, name="admin_credits.html",
        context={"entries": rows, "tiers": CREDIT_TIERS, "saved": saved},
    )


@router.post("/credits", response_class=HTMLResponse)
async def credits_manager_save(request: Request, _: str = Depends(_require_admin)):
    """Bulk save credit costs. Form field name: cost_<slug>."""
    form = await request.form()
    db = get_db()
    updated = 0
    for key, val in form.items():
        if not key.startswith("cost_"):
            continue
        slug = key[5:]
        try:
            cost = max(0.0, float(val))
        except (TypeError, ValueError):
            continue
        db.table("mcp_catalogue").update({
            "credit_cost_per_call": cost,
        }).eq("slug", slug).execute()
        updated += 1
    return RedirectResponse(url="/admin/credits?saved=true", status_code=303)


_BULK_ACTIONS = {
    "tier-standard": ("tier", "standard"),
    "tier-super": ("tier", "super"),
    "publish": ("is_published", True),
    "unpublish": ("is_published", False),
}


async def _upsert_railway_slug(db, slug: str) -> dict | None:
    """Create a minimal catalogue row for a Railway-only slug. Returns the row or None."""
    settings = get_settings()
    if not settings.RAILWAY_API_TOKEN:
        return None
    from src.admin.railway import fetch_railway_services
    services = await fetch_railway_services(
        settings.RAILWAY_API_TOKEN,
        project_id=settings.RAILWAY_PROJECT_ID,
        project_ids=settings.RAILWAY_PROJECT_IDS,
    )
    svc = next((s for s in services if s["slug"] == slug), None)
    if svc is None:
        return None
    upstream_url = svc["upstream_url"] or ""
    description, tool_count = await _auto_describe_mcp(upstream_url, "", svc["name"])
    inserted = db.table("mcp_catalogue").insert({
        "slug": slug,
        "name": svc["name"],
        "description": svc["name"],
        "description_agent": description or "",
        "tool_count": tool_count,
        "category": "MCP Server",
        "upstream_url": upstream_url,
        "upstream_api_key": "",
        "is_published": False,
    }).execute()
    return inserted.data[0] if inserted.data else None


@router.post("/catalogue/bulk", response_class=HTMLResponse)
async def bulk_catalogue(
    request: Request,
    _: str = Depends(_require_admin),
):
    form = await request.form()
    action = form.get("action", "")
    slugs = [s for s in form.getlist("slugs") if s]
    if action not in _BULK_ACTIONS or not slugs:
        return RedirectResponse(url="/admin/catalogue", status_code=303)

    column, value = _BULK_ACTIONS[action]
    db = get_db()
    for slug in slugs:
        row = _get_catalogue_row(db, slug)
        if row is None:
            row = await _upsert_railway_slug(db, slug)
            if row is None:
                continue
        db.table("mcp_catalogue").update({column: value}).eq("slug", slug).execute()
    return RedirectResponse(url="/admin/catalogue", status_code=303)


# ── Categories ───────────────────────────────────────────────────────────────


def _list_categories(db) -> list[dict]:
    result = (
        db.table("mcp_categories")
        .select("*")
        .order("sort_order")
        .order("name")
        .execute()
    )
    return result.data or []


@router.get("/categories", response_class=HTMLResponse)
async def list_categories(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    return templates.TemplateResponse(
        request=request, name="categories_list.html",
        context={"categories": _list_categories(db)},
    )


@router.get("/categories/new", response_class=HTMLResponse)
async def new_category_form(request: Request, _: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="categories_form.html", context={"error": None}
    )


@router.post("/categories", response_class=HTMLResponse)
async def create_category(
    request: Request,
    name: str = Form(...),
    sort_order: int = Form(0),
    _: str = Depends(_require_admin),
):
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            request=request, name="categories_form.html",
            context={"error": "Name required"},
        )
    db = get_db()
    existing = db.table("mcp_categories").select("name").eq("name", name).limit(1).execute()
    if existing.data:
        return templates.TemplateResponse(
            request=request, name="categories_form.html",
            context={"error": f"Category '{name}' already exists"},
        )
    db.table("mcp_categories").insert({"name": name, "sort_order": sort_order}).execute()
    return RedirectResponse(url="/admin/categories", status_code=303)


@router.post("/categories/{name}/delete", response_class=HTMLResponse)
async def delete_category(request: Request, name: str, _: str = Depends(_require_admin)):
    db = get_db()
    db.table("mcp_categories").delete().eq("name", name).execute()
    return RedirectResponse(url="/admin/categories", status_code=303)


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


# ── Users (tenants) ──────────────────────────────────────────────────────────


@router.get("/users/", response_class=HTMLResponse)
async def list_users(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    result = (
        db.table("users")
        .select("*")
        .order("created_at", desc=True)
        .execute()
    )
    users = result.data or []
    # Attach device count per user
    for u in users:
        u["device_count"] = (
            db.table("oauth_clients").select("*", count="exact")
            .eq("user_id", u["user_id"]).execute().count or 0
        )
    return templates.TemplateResponse(
        request=request,
        name="users_list.html",
        context={"users": users},
    )


@router.get("/users/new", response_class=HTMLResponse)
async def new_user_form(request: Request, _: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="user_create.html", context={"error": None}
    )


@router.post("/users", response_class=HTMLResponse)
async def create_user_admin(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    initial_credits: float = Form(5.0),
    _: str = Depends(_require_admin),
):
    from src.users import SupabaseUserProvider
    users = SupabaseUserProvider()
    try:
        user = users.create_user(
            email=email,
            display_name=display_name or None,
            credit_balance=initial_credits,
            is_active=False,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="user_create.html",
            context={"error": str(exc)},
            status_code=400,
        )

    db = get_db()
    client_id = generate_client_id()
    raw_secret = generate_token(32)
    secret_hash = hash_secret(raw_secret)
    db.table("oauth_clients").insert({
        "client_id": client_id,
        "client_secret_hash": secret_hash,
        "client_name": display_name or email,
        "redirect_uris": [],
        "grant_types": ["authorization_code"],
        "scope": "mcp",
        "allowed_mcp_resources": [],
        "created_by": email,
        "is_active": True,
        "user_id": user.user_id,
        "claimed_at": "now()",
    }).execute()

    from src.portal.routes import create_setup_token
    setup_token = create_setup_token(user.user_id)

    import asyncio
    try:
        asyncio.create_task(em.send_approval_email(
            contact_name=display_name or email,
            contact_email=email,
            company_name=display_name or email,
            user_id=user.user_id,
            issuer_url=get_settings().OAUTH_ISSUER_URL,
            setup_token=setup_token,
        ))
    except Exception as exc:
        import sys
        print(f"WARNING: setup email failed: {exc}", file=sys.stderr)

    return RedirectResponse(
        url=f"/admin/users/{user.user_id}?secret={raw_secret}&client_id={client_id}",
        status_code=303,
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(
    request: Request,
    user_id: str,
    secret: Optional[str] = None,
    client_id: Optional[str] = None,
    _: str = Depends(_require_admin),
):
    from src.users import SupabaseUserProvider
    users = SupabaseUserProvider()
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    devices = users.list_user_clients(user_id)

    db = get_db()
    today_start = datetime.datetime.utcnow().strftime("%Y-%m-%dT00:00:00Z")
    month_start = datetime.datetime.utcnow().strftime("%Y-%m-01T00:00:00Z")
    usage_today = (
        db.table("oauth_usage_logs").select("*", count="exact")
        .eq("user_id", user_id).gte("called_at", today_start).execute().count or 0
    )
    usage_month = (
        db.table("oauth_usage_logs").select("*", count="exact")
        .eq("user_id", user_id).gte("called_at", month_start).execute().count or 0
    )

    # Effective allowed MCPs: prune stored list, drop inaccessible slugs
    effective_allowed = users.prune_allowed_mcps(user_id)

    return templates.TemplateResponse(
        request=request,
        name="user_detail.html",
        context={
            "user": user,
            "devices": devices,
            "secret": secret,
            "new_client_id": client_id,
            "usage_today": usage_today,
            "usage_month": usage_month,
            "effective_allowed_mcps": effective_allowed,
        },
    )


@router.post("/users/{user_id}/add-credits", response_class=HTMLResponse)
async def user_add_credits(
    user_id: str,
    amount: float = Form(...),
    _: str = Depends(_require_admin),
):
    from src.users import SupabaseUserProvider
    users = SupabaseUserProvider()
    if users.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    users.add_credits(user_id, amount)
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/set-tier", response_class=HTMLResponse)
async def user_set_tier(
    user_id: str,
    tier: str = Form(...),
    _: str = Depends(_require_admin),
):
    from src.users import SupabaseUserProvider
    users = SupabaseUserProvider()
    if users.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    if tier not in ("standard", "super"):
        raise HTTPException(status_code=400, detail="Invalid tier")
    get_db().table("users").update({"tier": tier}).eq("user_id", user_id).execute()
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/rekey-device", response_class=HTMLResponse)
async def user_rekey_device(
    user_id: str,
    client_id: str = Form(...),
    _: str = Depends(_require_admin),
):
    db = get_db()
    row = _get_client_row(db, client_id)
    if row is None or row.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Device not found for this user")
    raw_secret = generate_token(32)
    db.table("oauth_clients").update(
        {"client_secret_hash": hash_secret(raw_secret)}
    ).eq("client_id", client_id).execute()
    return RedirectResponse(
        url=f"/admin/users/{user_id}?secret={raw_secret}&client_id={client_id}",
        status_code=303,
    )


@router.post("/users/{user_id}/revoke-device", response_class=HTMLResponse)
async def user_revoke_device(
    user_id: str,
    client_id: str = Form(...),
    _: str = Depends(_require_admin),
):
    db = get_db()
    # Ensure the client belongs to this user before revoking
    row = _get_client_row(db, client_id)
    if row is None or row.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Device not found for this user")
    db.table("oauth_clients").update({"is_active": False}).eq("client_id", client_id).execute()
    provider = SupabaseOAuthProvider()
    provider.revoke_client_tokens(client_id)
    try:
        from src.gateway.routes import evict_transport
        evict_transport(client_id)
    except Exception:
        pass
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/delete-device", response_class=HTMLResponse)
async def user_delete_device(
    user_id: str,
    client_id: str = Form(...),
    _: str = Depends(_require_admin),
):
    db = get_db()
    # Ensure the client belongs to this user before hard-deleting
    row = _get_client_row(db, client_id)
    if row is None or row.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Device not found for this user")
    provider = SupabaseOAuthProvider()
    provider.delete_client(client_id)
    try:
        from src.gateway.routes import evict_transport
        evict_transport(client_id)
    except Exception:
        pass
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(
    user_id: str,
    _: str = Depends(_require_admin),
):
    from src.users import SupabaseUserProvider
    users = SupabaseUserProvider()
    if users.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    users.delete_user(user_id)  # FK cascade removes devices + tokens
    return RedirectResponse(url="/admin/users/", status_code=303)


@router.post("/users/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_users(
    request: Request,
    _: str = Depends(_require_admin),
):
    from src.users import SupabaseUserProvider
    form = await request.form()
    user_ids = [u for u in form.getlist("user_ids") if u]
    if not user_ids:
        return RedirectResponse(url="/admin/users/", status_code=303)
    users = SupabaseUserProvider()
    for uid in user_ids:
        if users.get_user(uid) is not None:
            users.delete_user(uid)
    return RedirectResponse(url="/admin/users/", status_code=303)


@router.post("/unclaimed/cleanup", response_class=HTMLResponse)
async def cleanup_unclaimed(_: str = Depends(_require_admin)):
    """Delete DCR clients older than 24h that nobody ever claimed."""
    db = get_db()
    cutoff_24h = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat() + "Z"
    db.table("oauth_clients").delete().is_("user_id", "null").lt(
        "created_at", cutoff_24h
    ).execute()
    return RedirectResponse(url="/admin/", status_code=303)


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


# ── Telegram test ─────────────────────────────────────────────────────────────

@router.post("/telegram/test")
async def telegram_test(_: str = Depends(_require_admin)):
    """Send a test Markdown message to the configured owner chat. Returns JSON."""
    from fastapi.responses import JSONResponse
    from src.admin.settings import get_setting
    import httpx as _httpx
    token = get_setting("telegram_bot_token") or get_settings().TELEGRAM_BOT_TOKEN
    chat_id = get_setting("telegram_chat_id") or get_settings().TELEGRAM_OWNER_CHAT_ID
    if not token or not chat_id:
        return JSONResponse({"ok": False, "error": "Bot Token and Owner Chat ID must both be set"}, status_code=400)
    try:
        async with _httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "✅ *DS-MOZ Connect* — test message from admin settings.",
                    "parse_mode": "Markdown",
                },
                timeout=10.0,
            )
        data = resp.json()
        if data.get("ok"):
            return JSONResponse({"ok": True, "message": "Test message sent — check your Telegram."})
        return JSONResponse({"ok": False, "error": data.get("description", "Unknown error")}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ── Credit top-up request queue ───────────────────────────────────────────────

@router.get("/topup-requests", response_class=HTMLResponse)
async def list_topup_requests(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    rows = (
        db.table("credit_topup_requests")
        .select("*")
        .order("created_at", desc=True)
        .limit(200)
        .execute()
    ).data or []
    return templates.TemplateResponse(
        request=request, name="topup_requests.html", context={"requests": rows}
    )


@router.post("/topup-requests/{request_id}/approve", response_class=HTMLResponse)
async def approve_topup(request_id: str, _: str = Depends(_require_admin)):
    db = get_db()
    row_res = (
        db.table("credit_topup_requests")
        .select("*")
        .eq("id", request_id)
        .limit(1)
        .execute()
    )
    if not row_res.data:
        raise HTTPException(status_code=404, detail="Request not found")
    row = row_res.data[0]
    if row["status"] != "pending":
        return RedirectResponse(url="/admin/topup-requests", status_code=303)
    user_id = row["user_id"]
    amount = float(row["amount"])
    provider = SupabaseOAuthProvider(db)
    user = provider.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    new_balance = float(user.credit_balance or 0) + amount
    db.table("users").update({"credit_balance": new_balance}).eq("user_id", user_id).execute()
    db.table("credit_topup_requests").update({
        "status": "approved",
        "reviewed_at": datetime.datetime.utcnow().isoformat(),
        "reviewed_by": "admin-web",
    }).eq("id", request_id).execute()
    return RedirectResponse(url="/admin/topup-requests", status_code=303)


@router.post("/topup-requests/{request_id}/reject", response_class=HTMLResponse)
async def reject_topup(request_id: str, _: str = Depends(_require_admin)):
    db = get_db()
    db.table("credit_topup_requests").update({
        "status": "rejected",
        "reviewed_at": datetime.datetime.utcnow().isoformat(),
        "reviewed_by": "admin-web",
    }).eq("id", request_id).execute()
    return RedirectResponse(url="/admin/topup-requests", status_code=303)


# ── Landing testimonials ───────────────────────────────────────────────────────

@router.get("/testimonials", response_class=HTMLResponse)
async def list_testimonials(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    rows = db.table("landing_testimonials").select("*").order("sort_order").execute().data or []
    return templates.TemplateResponse(request=request, name="admin_testimonials.html", context={"testimonials": rows})


@router.get("/testimonials/new", response_class=HTMLResponse)
async def new_testimonial_form(request: Request, _: str = Depends(_require_admin)):
    return templates.TemplateResponse(request=request, name="admin_testimonial_form.html", context={"item": None})


@router.post("/testimonials/new", response_class=HTMLResponse)
async def create_testimonial(
    request: Request,
    _: str = Depends(_require_admin),
    quote: str = Form(...),
    author_name: str = Form(...),
    author_role: str = Form(""),
    author_org: str = Form(""),
    author_initials: str = Form(""),
    sort_order: int = Form(0),
    is_active: str = Form("off"),
):
    db = get_db()
    db.table("landing_testimonials").insert({
        "quote": quote.strip(),
        "author_name": author_name.strip(),
        "author_role": author_role.strip() or None,
        "author_org": author_org.strip() or None,
        "author_initials": author_initials.strip() or None,
        "sort_order": sort_order,
        "is_active": is_active == "on",
    }).execute()
    return RedirectResponse(url="/admin/testimonials", status_code=303)


@router.get("/testimonials/{item_id}/edit", response_class=HTMLResponse)
async def edit_testimonial_form(item_id: str, request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    result = db.table("landing_testimonials").select("*").eq("id", item_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Testimonial not found")
    return templates.TemplateResponse(request=request, name="admin_testimonial_form.html", context={"item": result.data[0]})


@router.post("/testimonials/{item_id}/edit", response_class=HTMLResponse)
async def update_testimonial(
    item_id: str,
    _: str = Depends(_require_admin),
    quote: str = Form(...),
    author_name: str = Form(...),
    author_role: str = Form(""),
    author_org: str = Form(""),
    author_initials: str = Form(""),
    sort_order: int = Form(0),
    is_active: str = Form("off"),
):
    db = get_db()
    db.table("landing_testimonials").update({
        "quote": quote.strip(),
        "author_name": author_name.strip(),
        "author_role": author_role.strip() or None,
        "author_org": author_org.strip() or None,
        "author_initials": author_initials.strip() or None,
        "sort_order": sort_order,
        "is_active": is_active == "on",
    }).eq("id", item_id).execute()
    return RedirectResponse(url="/admin/testimonials", status_code=303)


@router.post("/testimonials/{item_id}/delete")
async def delete_testimonial(item_id: str, _: str = Depends(_require_admin)):
    db = get_db()
    db.table("landing_testimonials").delete().eq("id", item_id).execute()
    return RedirectResponse(url="/admin/testimonials", status_code=303)


# ── Landing partners ──────────────────────────────────────────────────────────

@router.get("/partners", response_class=HTMLResponse)
async def list_partners(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    rows = db.table("landing_partners").select("*").order("sort_order").execute().data or []
    return templates.TemplateResponse(request=request, name="admin_partners.html", context={"partners": rows})


@router.get("/partners/new", response_class=HTMLResponse)
async def new_partner_form(request: Request, _: str = Depends(_require_admin)):
    return templates.TemplateResponse(request=request, name="admin_partner_form.html", context={"item": None})


@router.post("/partners/new", response_class=HTMLResponse)
async def create_partner(
    request: Request,
    _: str = Depends(_require_admin),
    name: str = Form(...),
    logo_url: str = Form(""),
    website_url: str = Form(""),
    sort_order: int = Form(0),
    is_active: str = Form("off"),
):
    db = get_db()
    db.table("landing_partners").insert({
        "name": name.strip(),
        "logo_url": logo_url.strip() or None,
        "website_url": website_url.strip() or None,
        "sort_order": sort_order,
        "is_active": is_active == "on",
    }).execute()
    return RedirectResponse(url="/admin/partners", status_code=303)


@router.get("/partners/{item_id}/edit", response_class=HTMLResponse)
async def edit_partner_form(item_id: str, request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    result = db.table("landing_partners").select("*").eq("id", item_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Partner not found")
    return templates.TemplateResponse(request=request, name="admin_partner_form.html", context={"item": result.data[0]})


@router.post("/partners/{item_id}/edit", response_class=HTMLResponse)
async def update_partner(
    item_id: str,
    _: str = Depends(_require_admin),
    name: str = Form(...),
    logo_url: str = Form(""),
    website_url: str = Form(""),
    sort_order: int = Form(0),
    is_active: str = Form("off"),
):
    db = get_db()
    db.table("landing_partners").update({
        "name": name.strip(),
        "logo_url": logo_url.strip() or None,
        "website_url": website_url.strip() or None,
        "sort_order": sort_order,
        "is_active": is_active == "on",
    }).eq("id", item_id).execute()
    return RedirectResponse(url="/admin/partners", status_code=303)


@router.post("/partners/{item_id}/delete")
async def delete_partner(item_id: str, _: str = Depends(_require_admin)):
    db = get_db()
    db.table("landing_partners").delete().eq("id", item_id).execute()
    return RedirectResponse(url="/admin/partners", status_code=303)

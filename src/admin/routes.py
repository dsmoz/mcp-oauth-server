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
from src.crypto import generate_client_id, generate_token, hash_secret, now_unix
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
        },
    )


# ── Client list ───────────────────────────────────────────────────────────────

@router.get("/clients/", response_class=HTMLResponse)
async def list_clients(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    result = db.table("oauth_clients").select("*").order("created_at", desc=True).execute()
    clients = result.data or []
    return templates.TemplateResponse(
        request=request,
        name="clients_list.html",
        context={"clients": clients},
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
    _: str = Depends(_require_admin),
):
    db = get_db()
    client = _get_client_row(db, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return templates.TemplateResponse(
        request=request,
        name="client_detail.html",
        context={"client": client, "secret": secret},
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
    }).execute()

    db.table("oauth_registration_requests").update({
        "status": "approved",
        "reviewed_at": "now()",
        "reviewed_by": admin,
    }).eq("id", request_id).execute()

    import asyncio
    try:
        asyncio.create_task(em.send_approval_email(
            contact_name=reg.get("contact_name", reg["contact_email"]),
            contact_email=reg["contact_email"],
            company_name=reg["company_name"],
            client_id=client_id,
            raw_secret=raw_secret,
            issuer_url=get_settings().OAUTH_ISSUER_URL,
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

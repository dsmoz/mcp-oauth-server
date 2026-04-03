from __future__ import annotations

import os as _os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from src.config import get_settings
from src.crypto import generate_client_id, generate_token, hash_secret
from src.db import get_db
from src.oauth.provider import SupabaseOAuthProvider

router = APIRouter(prefix="/admin")

_TEMPLATES_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)
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


# ── List clients ──────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
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

    # Redirect to detail page with secret shown once
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
    result = (
        db.table("oauth_clients")
        .select("*")
        .eq("client_id", client_id)
        .maybe_single()
        .execute()
    )
    if result.data is None:
        raise HTTPException(status_code=404, detail="Client not found")

    client = result.data
    return templates.TemplateResponse(
        request=request,
        name="client_detail.html",
        context={"client": client, "secret": secret},
    )


# ── Revoke client ─────────────────────────────────────────────────────────────

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

    return RedirectResponse(url="/admin/", status_code=303)

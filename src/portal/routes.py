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
    """Validate token, return client_id if valid and unused."""
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

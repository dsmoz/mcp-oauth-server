from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config import get_settings
from src.crypto import generate_client_id, generate_token, hash_secret, hash_token, verify_secret
from src.db import get_db
from src.users.provider import SupabaseUserProvider

router = APIRouter(prefix="/portal")

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

_SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours
_COOKIE_NAME = "portal_session"
_SETUP_TOKEN_TTL_HOURS = 24
_COOKIE_SECURE = get_settings().OAUTH_ISSUER_URL.startswith("https://")


def _oauth_success_page(redirect: str | None = None) -> str:
    js = f"window.location.href={redirect!r};" if redirect else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">
<script>{js}</script>
<style>
  body{{font-family:'Segoe UI',sans-serif;background:#f4f6f8;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
  .card{{background:#fff;border-radius:10px;padding:2rem 2.5rem;box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center;max-width:400px}}
  .icon{{font-size:2.5rem;color:#22c55e;margin-bottom:1rem}}
  h1{{font-size:1.2rem;color:#0A1C20;margin:0 0 .5rem}}
  p{{color:#5A8A90;font-size:.9rem;margin:0}}
</style>
</head>
<body>
<div class="card">
  <div class="icon">&#10003;</div>
  <h1>Authorisation complete</h1>
  <p>You are now connected. You can close this window and return to Claude.</p>
</div>
</body>
</html>"""


# ── Session (keyed on user_id) ────────────────────────────────────────────────

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().SECRET_KEY, salt="portal")


def _sign_session(user_id: str) -> str:
    return _serializer().dumps({"user_id": user_id})


def _verify_session(token: str) -> Optional[str]:
    try:
        data = _serializer().loads(token, max_age=_SESSION_MAX_AGE)
        return data.get("user_id")
    except (BadSignature, SignatureExpired, KeyError):
        return None


def _require_portal_user(request: Request) -> str:
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/portal/login"})
    user_id = _verify_session(token)
    if not user_id:
        raise HTTPException(status_code=302, headers={"Location": "/portal/login"})
    return user_id


def _users() -> SupabaseUserProvider:
    return SupabaseUserProvider()


def _list_devices(user_id: str) -> list[dict]:
    return _users().list_user_clients(user_id)


# ── Setup tokens (keyed on user_id) ───────────────────────────────────────────

def _hash_setup_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_setup_token(user_id: str) -> str:
    """Generate a one-time setup token keyed on user_id, return raw token."""
    raw = generate_token(32)
    token_hash = _hash_setup_token(raw)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=_SETUP_TOKEN_TTL_HOURS)).isoformat()
    get_db().table("portal_setup_tokens").insert({
        "user_id": user_id,
        "token_hash": token_hash,
        "expires_at": expires_at,
    }).execute()
    return raw


def _redeem_setup_token(raw: str) -> Optional[str]:
    """Return user_id if token is valid and unused."""
    token_hash = _hash_setup_token(raw)
    db = get_db()
    result = (
        db.table("portal_setup_tokens")
        .select("*")
        .eq("token_hash", token_hash)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]
    if row.get("used_at"):
        return None
    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires_at:
        return None
    return row.get("user_id") or row.get("client_id")  # fallback for legacy tokens


def _consume_setup_token(raw: str) -> None:
    token_hash = _hash_setup_token(raw)
    get_db().table("portal_setup_tokens").update({
        "used_at": datetime.now(timezone.utc).isoformat()
    }).eq("token_hash", token_hash).execute()


# ── OAuth session completion + device claim ──────────────────────────────────

def _complete_oauth_session(next_session: str, user_id: str) -> Optional[RedirectResponse | HTMLResponse]:
    """Claim the pending session's client for this user, then mark approved and redirect.
    Returns None if the session is expired or missing."""
    from src.oauth.provider import SupabaseOAuthProvider
    import json as _json
    import sys

    provider = SupabaseOAuthProvider()
    pending = provider.get_pending_session(next_session)
    if pending is None:
        return None

    pending_client_id = pending.get("client_id")
    if pending_client_id:
        try:
            provider.claim_unclaimed_client(pending_client_id, user_id)
        except ValueError as exc:
            print(f"PORTAL: claim conflict on {pending_client_id} → {exc}", file=sys.stderr)
            return HTMLResponse(
                f"<h1>Device already connected to another account</h1>"
                f"<p>This device is linked to a different user. Please sign out and retry on a fresh device.</p>",
                status_code=409,
            )

    try:
        session_data = _json.loads(pending.get("resource") or "{}")
    except (ValueError, TypeError):
        session_data = {}
    state = session_data.get("state")

    try:
        code, redirect_uri = provider.mark_session_approved(next_session)
    except ValueError:
        return None

    if not redirect_uri:
        return HTMLResponse(_oauth_success_page())
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(url=location, status_code=302)


# ── Login ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def portal_login_get(
    request: Request,
    next_session: Optional[str] = Query(None),
):
    # Auto-complete OAuth if user already has a valid portal session
    if next_session:
        token = request.cookies.get(_COOKIE_NAME)
        if token:
            user_id = _verify_session(token)
            if user_id:
                response = _complete_oauth_session(next_session, user_id)
                if response is not None:
                    return response
    return templates.TemplateResponse(
        request=request,
        name="portal_login.html",
        context={"error": None, "next_session": next_session},
    )


@router.post("/login", response_class=HTMLResponse)
async def portal_login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_session: Optional[str] = Query(None),
):
    email = username.strip().lower()
    users = _users()
    user = users.get_user_by_email(email)

    def _login_error(msg: str, status: int = 401):
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": msg, "next_session": next_session}, status_code=status,
        )

    if user is None:
        return _login_error("Invalid email or password")
    if not user.password_hash:
        return _login_error(
            "Account not yet set up. Please use the setup link from your registration email."
        )
    if not users.verify_password(user, password):
        return _login_error("Invalid email or password")
    if not user.is_active:
        return _login_error("Account is not active. Contact support.", status=403)

    cookie_value = _sign_session(user.user_id)

    # OAuth flow: claim pending client, complete auth session, redirect back
    if next_session:
        response = _complete_oauth_session(next_session, user.user_id)
        if response is None:
            response = RedirectResponse(url="/portal/?oauth_expired=1", status_code=303)
        response.set_cookie(
            _COOKIE_NAME, cookie_value,
            httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE, secure=_COOKIE_SECURE,
        )
        return response

    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(
        _COOKIE_NAME, cookie_value,
        httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE, secure=_COOKIE_SECURE,
    )
    return response


# ── Plugin JSON login ────────────────────────────────────────────────────────

def _ensure_plugin_client(user_id: str, display_name: str) -> tuple[str, str]:
    """Find-or-create the user's plugin-default oauth_client. Returns (client_id, client_secret_hash).
    We don't return a raw secret here — plugin clients use bearer tokens only."""
    db = get_db()
    existing = (
        db.table("oauth_clients")
        .select("client_id, client_secret_hash")
        .eq("user_id", user_id)
        .eq("client_name", "dsmoz plugin")
        .limit(1)
        .execute()
    )
    if existing.data:
        row = existing.data[0]
        return row["client_id"], row["client_secret_hash"]

    client_id = generate_client_id()
    raw_secret = generate_token(32)
    secret_hash = hash_secret(raw_secret)
    claimed_at = datetime.now(timezone.utc).isoformat()
    db.table("oauth_clients").insert({
        "client_id": client_id,
        "client_secret_hash": secret_hash,
        "client_name": "dsmoz plugin",
        "redirect_uris": [],
        "grant_types": ["authorization_code"],
        "scope": "mcp",
        "created_by": f"plugin:{display_name}",
        "is_active": True,
        "user_id": user_id,
        "claimed_at": claimed_at,
    }).execute()
    return client_id, secret_hash


@router.post("/api/login")
async def plugin_login(request: Request):
    """JSON login for the Zotero plugin. Authenticates by email+password and returns a user-scoped token."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    email = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return JSONResponse({"error": "username and password required"}, status_code=400)

    users = _users()
    user = users.get_user_by_email(email)
    if user is None:
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)
    if not user.password_hash:
        return JSONResponse({
            "error": "Account not yet set up",
            "action": "setup",
            "message": "Please use the setup link from your registration email.",
        }, status_code=403)
    if not users.verify_password(user, password):
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)

    client_id, _ = _ensure_plugin_client(user.user_id, user.display_name or email)

    access_token = generate_token(32)
    get_db().table("oauth_access_tokens").insert({
        "token": hash_token(access_token),
        "client_id": client_id,
        "user_id": user.user_id,
        "scopes": ["mcp"],
        "expires_at": 0,
        "is_revoked": False,
    }).execute()

    return JSONResponse({
        "success": True,
        "client_id": client_id,
        "user_id": user.user_id,
        "access_token": access_token,
        "expires_in": 0,
        "display_name": user.display_name or email,
    })


# ── Setup password (first login) ──────────────────────────────────────────────

@router.get("/setup-password", response_class=HTMLResponse)
async def setup_password_get(request: Request, token: str = ""):
    user_id = _redeem_setup_token(token)
    if not user_id:
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Setup link is invalid or has expired. Contact your administrator."},
        )
    user = _users().get_user(user_id)
    if user is None:
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Account not found."},
        )
    return templates.TemplateResponse(
        request=request, name="portal_setup_password.html",
        context={"token": token, "username": user.email, "client_id": user.user_id, "error": None},
    )


@router.post("/setup-password", response_class=HTMLResponse)
async def setup_password_post(
    request: Request,
    token: str = Form(...),
    username: str = Form(...),  # kept for template compatibility; ignored (email is identity)
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    user_id = _redeem_setup_token(token)
    if not user_id:
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Setup link is invalid or has expired. Contact your administrator."},
        )

    users = _users()
    user = users.get_user(user_id)
    if user is None:
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Account not found."},
        )

    def _err(msg: str):
        return templates.TemplateResponse(
            request=request, name="portal_setup_password.html",
            context={"token": token, "username": user.email, "client_id": user.user_id, "error": msg},
        )

    if password != password_confirm:
        return _err("Passwords do not match")
    if len(password) < 8:
        return _err("Password must be at least 8 characters")

    users.set_password(user.user_id, password)
    _consume_setup_token(token)

    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(
        _COOKIE_NAME, _sign_session(user.user_id),
        httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE,
    )
    return response


@router.post("/logout")
async def portal_logout():
    response = RedirectResponse(url="/portal/login", status_code=303)
    response.delete_cookie(_COOKIE_NAME)
    return response


# ── Password reset ────────────────────────────────────────────────────────────

@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_get(request: Request):
    return templates.TemplateResponse(
        request=request, name="portal_forgot_password.html",
        context={"sent": False, "error": None},
    )


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_post(request: Request, email: str = Form(...)):
    identifier = email.strip().lower()
    user = _users().get_user_by_email(identifier)
    # Always show the same "sent" page to prevent email enumeration
    if user is not None:
        raw = create_setup_token(user.user_id)
        issuer_url = get_settings().OAUTH_ISSUER_URL
        reset_url = f"{issuer_url}/portal/reset-password?token={raw}"
        try:
            from src import email as em
            await em.send_password_reset_email(
                contact_name=user.display_name or "there",
                contact_email=user.email,
                reset_url=reset_url,
            )
        except Exception as exc:
            import sys
            print(f"WARNING: password reset email failed: {exc}", file=sys.stderr)
    return templates.TemplateResponse(
        request=request, name="portal_forgot_password.html",
        context={"sent": True, "error": None},
    )


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_get(request: Request, token: str = ""):
    user_id = _redeem_setup_token(token)
    if not user_id:
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Reset link is invalid or has expired. Please request a new one."},
        )
    return templates.TemplateResponse(
        request=request, name="portal_reset_password.html",
        context={"token": token, "error": None},
    )


@router.post("/reset-password", response_class=HTMLResponse)
async def reset_password_post(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    user_id = _redeem_setup_token(token)
    if not user_id:
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": "Reset link is invalid or has expired. Please request a new one."},
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            request=request, name="portal_reset_password.html",
            context={"token": token, "error": "Passwords do not match"},
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request=request, name="portal_reset_password.html",
            context={"token": token, "error": "Password must be at least 8 characters"},
        )

    _users().set_password(user_id, password)
    _consume_setup_token(token)

    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(
        _COOKIE_NAME, _sign_session(user_id),
        httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE,
    )
    return response


# ── Overview ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def portal_overview(
    request: Request,
    oauth_expired: Optional[str] = Query(None),
    user_id: str = Depends(_require_portal_user),
):
    users = _users()
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    db = get_db()
    from datetime import date
    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    def _count(query_result) -> int:
        return query_result.data[0]["count"] if query_result.data else 0

    usage_today = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("user_id", user_id).gte("called_at", today).execute()
    )
    usage_month = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("user_id", user_id).gte("called_at", month_start).execute()
    )
    usage_total = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("user_id", user_id).execute()
    )

    gateway_url = f"{get_settings().OAUTH_ISSUER_URL}/gateway/{user_id}"
    devices = _list_devices(user_id)

    # Template compatibility: expose a `client` dict mirroring the old shape
    client_ctx = {
        "client_id": user_id,
        "client_name": user.display_name or user.email,
        "portal_username": user.email,
        "credit_balance": user.credit_balance,
        "allowed_mcp_resources": user.allowed_mcp_resources,
        "is_active": user.is_active,
    }

    return templates.TemplateResponse(
        request=request, name="portal_overview.html", context={
            "client": client_ctx,
            "user": user,
            "devices": devices,
            "active_nav": "overview",
            "usage_today": usage_today,
            "usage_month": usage_month,
            "usage_total": usage_total,
            "gateway_url": gateway_url,
            "credit_balance": float(user.credit_balance or 0),
            "oauth_expired": bool(oauth_expired),
        }
    )


# ── MCP selection ─────────────────────────────────────────────────────────────

@router.get("/mcps", response_class=HTMLResponse)
async def portal_mcps_get(request: Request, user_id: str = Depends(_require_portal_user)):
    user = _users().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    db = get_db()
    catalogue = (
        db.table("mcp_catalogue").select("*").eq("is_published", True).order("name").execute().data
        or []
    )
    enabled = set(user.allowed_mcp_resources or [])

    client_ctx = {
        "client_id": user_id,
        "client_name": user.display_name or user.email,
        "portal_username": user.email,
    }

    return templates.TemplateResponse(
        request=request, name="portal_mcps.html", context={
            "client": client_ctx,
            "user": user,
            "active_nav": "mcps",
            "catalogue": catalogue,
            "enabled": enabled,
        }
    )


@router.post("/mcps", response_class=HTMLResponse)
async def portal_mcps_post(request: Request, user_id: str = Depends(_require_portal_user)):
    db = get_db()
    form = await request.form()
    catalogue = db.table("mcp_catalogue").select("slug").eq("is_published", True).execute().data or []
    valid_slugs = {row["slug"] for row in catalogue}
    selected = [slug for slug in form.getlist("mcps") if slug in valid_slugs]

    _users().set_allowed_mcps(user_id, selected)
    return RedirectResponse(url="/portal/mcps", status_code=303)


# ── Setup guide ───────────────────────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def portal_setup(request: Request, user_id: str = Depends(_require_portal_user)):
    user = _users().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    settings = get_settings()
    gateway_url = f"{settings.OAUTH_ISSUER_URL}/gateway/{user_id}"
    streamable_url = f"{settings.OAUTH_ISSUER_URL}/gateway/{user_id}/mcp"
    new_secret = request.query_params.get("secret")
    rotated_client_id = request.query_params.get("client_id")
    devices = _list_devices(user_id)

    client_ctx = {
        "client_id": user_id,
        "client_name": user.display_name or user.email,
        "portal_username": user.email,
    }

    return templates.TemplateResponse(
        request=request, name="portal_setup.html", context={
            "client": client_ctx,
            "user": user,
            "devices": devices,
            "active_nav": "setup",
            "gateway_url": gateway_url,
            "streamable_url": streamable_url,
            "client_id": user_id,
            "new_secret": new_secret,
            "rotated_client_id": rotated_client_id,
        }
    )


@router.post("/setup/rotate-secret")
async def portal_rotate_secret(
    user_id: str = Depends(_require_portal_user),
    client_id: str = Form(...),
):
    """Rotate a specific device's client secret. client_id must be owned by the user."""
    db = get_db()
    owner = (
        db.table("oauth_clients")
        .select("client_id")
        .eq("client_id", client_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not owner.data:
        raise HTTPException(status_code=404, detail="Device not found")

    raw = generate_token(32)
    db.table("oauth_clients").update(
        {"client_secret_hash": hash_secret(raw)}
    ).eq("client_id", client_id).execute()
    return RedirectResponse(
        url=f"/portal/setup?secret={raw}&client_id={client_id}", status_code=303,
    )


@router.post("/setup/revoke-device")
async def portal_revoke_device(
    user_id: str = Depends(_require_portal_user),
    client_id: str = Form(...),
):
    """Deactivate a device. Revokes its tokens by setting is_active=False."""
    db = get_db()
    owner = (
        db.table("oauth_clients")
        .select("client_id")
        .eq("client_id", client_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not owner.data:
        raise HTTPException(status_code=404, detail="Device not found")

    db.table("oauth_clients").update({"is_active": False}).eq("client_id", client_id).execute()
    db.table("oauth_access_tokens").update({"is_revoked": True}).eq("client_id", client_id).execute()
    db.table("oauth_refresh_tokens").update({"is_revoked": True}).eq("client_id", client_id).execute()
    return RedirectResponse(url="/portal/setup", status_code=303)


@router.get("/setup/download")
async def portal_setup_download(user_id: str = Depends(_require_portal_user)):
    import json
    from fastapi.responses import Response
    user = _users().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    settings = get_settings()
    gateway_url = f"{settings.OAUTH_ISSUER_URL}/gateway/{user_id}"
    server_name = (user.display_name or "dsmoz-intelligence").lower().replace(" ", "-")

    config = {
        "mcpServers": {
            server_name: {
                "command": "npx",
                "args": ["-y", "mcp-remote@latest", gateway_url],
            }
        }
    }

    return Response(
        content=json.dumps(config, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=claude_desktop_config.json"},
    )


# ── Credits ───────────────────────────────────────────────────────────────────

_CREDIT_PLANS = {
    "starter": 10.0,
    "pro": 50.0,
    "enterprise": 200.0,
}


@router.get("/credits", response_class=HTMLResponse)
async def portal_credits_get(
    request: Request,
    user_id: str = Depends(_require_portal_user),
    success: str = "",
):
    user = _users().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    client_ctx = {
        "client_id": user_id,
        "client_name": user.display_name or user.email,
        "credit_balance": user.credit_balance,
    }
    return templates.TemplateResponse(
        request=request, name="portal_credits.html", context={
            "client": client_ctx,
            "user": user,
            "active_nav": "credits",
            "credit_balance": float(user.credit_balance or 0),
            "success": success,
        }
    )


@router.post("/credits/buy", response_class=HTMLResponse)
async def portal_credits_buy(
    request: Request,
    plan: str = Form(...),
    user_id: str = Depends(_require_portal_user),
):
    credits_to_add = _CREDIT_PLANS.get(plan)
    if not credits_to_add:
        raise HTTPException(status_code=400, detail="Invalid plan")

    users = _users()
    users.add_credits(user_id, credits_to_add)
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    client_ctx = {
        "client_id": user_id,
        "client_name": user.display_name or user.email,
        "credit_balance": user.credit_balance,
    }
    return templates.TemplateResponse(
        request=request, name="portal_credits.html", context={
            "client": client_ctx,
            "user": user,
            "active_nav": "credits",
            "credit_balance": float(user.credit_balance or 0),
            "success": f"{credits_to_add:.0f} credits added to your account. New balance: {user.credit_balance:.2f}",
        }
    )

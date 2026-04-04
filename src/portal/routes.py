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
async def portal_login_get(
    request: Request,
    next_session: Optional[str] = Query(None),
):
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
    db = get_db()
    identifier = username.strip().lower()
    # Try username first, then fall back to email (stored in created_by)
    result = db.table("oauth_clients").select("*").eq("portal_username", identifier).eq("is_active", True).limit(1).execute()
    if not result.data:
        result = db.table("oauth_clients").select("*").eq("created_by", identifier).eq("is_active", True).limit(1).execute()
    client = result.data[0] if result.data else None

    def _login_error(msg: str):
        return templates.TemplateResponse(
            request=request, name="portal_login.html",
            context={"error": msg, "next_session": next_session}, status_code=401,
        )

    if client is None:
        return _login_error("Invalid username or password")
    if not client.get("portal_password_hash"):
        return _login_error("Account not yet set up. Please use the setup link from your registration email.")
    if not verify_secret(password, client["portal_password_hash"]):
        return _login_error("Invalid username or password")

    # Build the session cookie (used in all success paths)
    cookie_value = _sign_session(client["client_id"])

    # OAuth flow: complete the pending authorization session and redirect back to the client
    if next_session:
        from src.oauth.provider import SupabaseOAuthProvider
        provider = SupabaseOAuthProvider()
        pending = provider.get_pending_session(next_session)
        if pending is None:
            # Session expired — user is logged in but needs to reconnect from Claude Code
            response = RedirectResponse(url="/portal/?oauth_expired=1", status_code=303)
            response.set_cookie(_COOKIE_NAME, cookie_value, httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE)
            return response
        import json as _json
        try:
            session_data = _json.loads(pending.get("resource") or "{}")
        except (ValueError, TypeError):
            session_data = {}
        state = session_data.get("state")
        try:
            code, redirect_uri = provider.mark_session_approved(next_session)
        except ValueError:
            response = RedirectResponse(url="/portal/?oauth_expired=1", status_code=303)
            response.set_cookie(_COOKIE_NAME, cookie_value, httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE)
            return response
        if not redirect_uri:
            response = HTMLResponse("<h1>Authorization complete. You may close this window.</h1>")
        else:
            sep = "&" if "?" in redirect_uri else "?"
            location = f"{redirect_uri}{sep}code={code}"
            if state:
                location += f"&state={state}"
            response = RedirectResponse(url=location, status_code=302)
        response.set_cookie(_COOKIE_NAME, cookie_value, httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE)
        return response

    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(_COOKIE_NAME, cookie_value, httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE)
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
        context={"token": token, "username": client.get("portal_username", ""), "client_id": client_id, "error": None},
    )


@router.get("/check-username")
async def check_username(username: str = Query(...), exclude_client_id: Optional[str] = Query(None)):
    """JSON endpoint: returns {available: bool} for live username availability check."""
    identifier = username.strip().lower()
    if not identifier or " " in identifier:
        return JSONResponse({"available": False})
    db = get_db()
    result = db.table("oauth_clients").select("client_id").eq("portal_username", identifier).limit(1).execute()
    if result.data:
        taken_by = result.data[0]["client_id"]
        # Allow if the only match is the current client (re-setting up their own account)
        if exclude_client_id and taken_by == exclude_client_id:
            return JSONResponse({"available": True})
        return JSONResponse({"available": False})
    return JSONResponse({"available": True})


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
    if " " in username:
        return templates.TemplateResponse(
            request=request, name="portal_setup_password.html",
            context={"token": token, "username": username, "client_id": client_id, "error": "Username cannot contain spaces"},
        )
    # Check uniqueness (exclude the current client's own existing username)
    db = get_db()
    taken = db.table("oauth_clients").select("client_id").eq("portal_username", username.strip().lower()).limit(1).execute()
    if taken.data and taken.data[0]["client_id"] != client_id:
        return templates.TemplateResponse(
            request=request, name="portal_setup_password.html",
            context={"token": token, "username": username, "client_id": client_id, "error": "Username is already taken. Please choose another."},
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            request=request, name="portal_setup_password.html",
            context={"token": token, "username": username, "client_id": client_id, "error": "Passwords do not match"},
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request=request, name="portal_setup_password.html",
            context={"token": token, "username": username, "client_id": client_id, "error": "Password must be at least 8 characters"},
        )

    db = get_db()
    db.table("oauth_clients").update({
        "portal_username": username.strip().lower(),
        "portal_password_hash": hash_secret(password),
        "is_active": True,
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


# ── Password reset ────────────────────────────────────────────────────────────

@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_get(request: Request):
    return templates.TemplateResponse(
        request=request, name="portal_forgot_password.html",
        context={"sent": False, "error": None},
    )


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_post(request: Request, email: str = Form(...)):
    db = get_db()
    identifier = email.strip().lower()
    result = db.table("oauth_clients").select("client_id,portal_username,created_by").eq("portal_username", identifier).eq("is_active", True).limit(1).execute()
    if not result.data:
        result = db.table("oauth_clients").select("client_id,portal_username,created_by").eq("created_by", identifier).eq("is_active", True).limit(1).execute()
    # Always show the same "sent" page to prevent email enumeration
    if result.data:
        client = result.data[0]
        raw = create_setup_token(client["client_id"])
        from src.config import get_settings as _gs
        from src import email as em
        issuer_url = _gs().OAUTH_ISSUER_URL
        reset_url = f"{issuer_url}/portal/reset-password?token={raw}"
        try:
            await em.send_password_reset_email(
                contact_name=client.get("portal_username") or "there",
                contact_email=client.get("created_by") or email.strip().lower(),
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
    client_id = _redeem_setup_token(token)
    if not client_id:
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
    client_id = _redeem_setup_token(token)
    if not client_id:
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

    get_db().table("oauth_clients").update({
        "portal_password_hash": hash_secret(password),
    }).eq("client_id", client_id).execute()
    _consume_setup_token(token)

    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(
        _COOKIE_NAME, _sign_session(client_id),
        httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE,
    )
    return response


# ── Overview ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def portal_overview(request: Request, oauth_expired: Optional[str] = Query(None), client_id: str = Depends(_require_portal_client)):
    client = _get_client(client_id)
    if not client:
        raise HTTPException(status_code=401, detail="Client not found")

    db = get_db()
    from datetime import date, timezone as _tz
    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    def _count(query_result) -> int:
        return query_result.data[0]["count"] if query_result.data else 0

    usage_today = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("client_id", client_id).gte("called_at", today).execute()
    )
    usage_month = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("client_id", client_id).gte("called_at", month_start).execute()
    )
    usage_total = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("client_id", client_id).execute()
    )

    from src.config import get_settings
    gateway_url = f"{get_settings().OAUTH_ISSUER_URL}/gateway/{client_id}"

    return templates.TemplateResponse(
        request=request, name="portal_overview.html", context={
            "client": client,
            "active_nav": "overview",
            "usage_today": usage_today,
            "usage_month": usage_month,
            "usage_total": usage_total,
            "gateway_url": gateway_url,
            "credit_balance": float(client.get("credit_balance") or 0),
            "oauth_expired": bool(oauth_expired),
        }
    )


# ── MCP selection ─────────────────────────────────────────────────────────────

@router.get("/mcps", response_class=HTMLResponse)
async def portal_mcps_get(request: Request, client_id: str = Depends(_require_portal_client)):
    client = _get_client(client_id)
    if not client:
        raise HTTPException(status_code=401, detail="Client not found")

    db = get_db()
    catalogue = db.table("mcp_catalogue").select("*").eq("is_published", True).order("name").execute().data or []
    enabled = set(client.get("allowed_mcp_resources") or [])

    return templates.TemplateResponse(
        request=request, name="portal_mcps.html", context={
            "client": client,
            "active_nav": "mcps",
            "catalogue": catalogue,
            "enabled": enabled,
        }
    )


@router.post("/mcps", response_class=HTMLResponse)
async def portal_mcps_post(request: Request, client_id: str = Depends(_require_portal_client)):
    client = _get_client(client_id)
    if not client:
        raise HTTPException(status_code=401, detail="Client not found")

    form = await request.form()
    db = get_db()
    catalogue = db.table("mcp_catalogue").select("slug").eq("is_published", True).execute().data or []
    valid_slugs = {row["slug"] for row in catalogue}

    selected = [slug for slug in form.getlist("mcps") if slug in valid_slugs]

    db.table("oauth_clients").update({
        "allowed_mcp_resources": selected,
    }).eq("client_id", client_id).execute()

    return RedirectResponse(url="/portal/mcps", status_code=303)


# ── Setup guide ───────────────────────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def portal_setup(request: Request, client_id: str = Depends(_require_portal_client)):
    client = _get_client(client_id)
    if not client:
        raise HTTPException(status_code=401, detail="Client not found")

    from src.config import get_settings
    gateway_url = f"{get_settings().OAUTH_ISSUER_URL}/gateway/{client_id}"

    return templates.TemplateResponse(
        request=request, name="portal_setup.html", context={
            "client": client,
            "active_nav": "setup",
            "gateway_url": gateway_url,
        }
    )


@router.get("/setup/download")
async def portal_setup_download(client_id: str = Depends(_require_portal_client)):
    import json
    from fastapi.responses import Response
    client = _get_client(client_id)
    if not client:
        raise HTTPException(status_code=401, detail="Client not found")

    from src.config import get_settings
    settings = get_settings()
    gateway_url = f"{settings.OAUTH_ISSUER_URL}/gateway/{client_id}"
    server_name = (client.get("client_name") or "dsmoz-intelligence").lower().replace(" ", "-")

    config = {
        "mcpServers": {
            server_name: {
                "type": "sse",
                "url": gateway_url,
                "headers": {
                    "Authorization": "Bearer <your-access-token>"
                }
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
    client_id: str = Depends(_require_portal_client),
    success: str = "",
):
    client = _get_client(client_id)
    if not client:
        raise HTTPException(status_code=401, detail="Client not found")
    return templates.TemplateResponse(
        request=request, name="portal_credits.html", context={
            "client": client,
            "active_nav": "credits",
            "credit_balance": float(client.get("credit_balance") or 0),
            "success": success,
        }
    )


@router.post("/credits/buy", response_class=HTMLResponse)
async def portal_credits_buy(
    request: Request,
    plan: str = Form(...),
    client_id: str = Depends(_require_portal_client),
):
    client = _get_client(client_id)
    if not client:
        raise HTTPException(status_code=401, detail="Client not found")

    credits_to_add = _CREDIT_PLANS.get(plan)
    if not credits_to_add:
        raise HTTPException(status_code=400, detail="Invalid plan")

    db = get_db()
    current = float(client.get("credit_balance") or 0)
    new_balance = current + credits_to_add
    db.table("oauth_clients").update({"credit_balance": new_balance}).eq("client_id", client_id).execute()

    return templates.TemplateResponse(
        request=request, name="portal_credits.html", context={
            "client": _get_client(client_id),
            "active_nav": "credits",
            "credit_balance": new_balance,
            "success": f"{credits_to_add:.0f} credits added to your account. New balance: {new_balance:.2f}",
        }
    )

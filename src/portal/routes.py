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
from src.users.agent_tokens import AgentTokenProvider
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
        # Public/multi-user clients (e.g. dsmoz-academia) skip the claim step
        # entirely — tokens bind to each authorising user via the auth code,
        # not to a single client-row owner.
        client_row = provider.get_client(pending_client_id)
        if client_row is not None and not client_row.is_public_client:
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
        code, redirect_uri = provider.mark_session_approved(next_session, user_id=user_id)
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
    from collections import defaultdict
    today_dt = date.today()
    today = today_dt.isoformat()
    yesterday_dt = today_dt - timedelta(days=1)
    yesterday = yesterday_dt.isoformat()
    month_start = today_dt.replace(day=1).isoformat()
    fourteen_days_ago = (today_dt - timedelta(days=13)).isoformat()

    def _count(query_result) -> int:
        return query_result.data[0]["count"] if query_result.data else 0

    usage_today = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("user_id", user_id).gte("called_at", today).execute()
    )
    usage_yesterday = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("user_id", user_id).gte("called_at", yesterday).lt("called_at", today).execute()
    )
    usage_month = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("user_id", user_id).gte("called_at", month_start).execute()
    )
    usage_total = _count(
        db.table("oauth_usage_logs").select("count", count="exact")
          .eq("user_id", user_id).execute()
    )

    # 14-day activity bars (oldest → newest)
    activity_rows = (
        db.table("oauth_usage_logs").select("called_at")
          .eq("user_id", user_id).gte("called_at", fourteen_days_ago)
          .execute().data or []
    )
    day_counts: dict[str, int] = defaultdict(int)
    for row in activity_rows:
        ca = row.get("called_at") or ""
        if len(ca) >= 10:
            day_counts[ca[:10]] += 1
    activity_14days = [
        day_counts.get((today_dt - timedelta(days=13 - i)).isoformat(), 0)
        for i in range(14)
    ]

    # Last call (time + client name)
    last_call_time_str: Optional[str] = None
    last_call_client: Optional[str] = None
    last_call_rows = (
        db.table("oauth_usage_logs").select("called_at, client_id")
          .eq("user_id", user_id).order("called_at", desc=True).limit(1)
          .execute().data or []
    )
    if last_call_rows:
        try:
            ca = last_call_rows[0]["called_at"].replace("Z", "+00:00")
            last_dt = datetime.fromisoformat(ca)
            now = datetime.now(timezone.utc)
            secs = int((now - last_dt).total_seconds())
            if secs < 60:
                last_call_time_str = "just now"
            elif secs < 3600:
                m = secs // 60
                last_call_time_str = f"{m} minute{'s' if m != 1 else ''} ago"
            elif secs < 86400:
                h = secs // 3600
                last_call_time_str = f"{h} hour{'s' if h != 1 else ''} ago"
            else:
                d = secs // 86400
                last_call_time_str = f"{d} day{'s' if d != 1 else ''} ago"
        except Exception:
            pass
        cid = last_call_rows[0].get("client_id")
        if cid:
            try:
                cli = (
                    db.table("oauth_clients").select("client_name")
                      .eq("client_id", cid).limit(1).execute().data or []
                )
                if cli:
                    last_call_client = cli[0].get("client_name")
            except Exception:
                pass

    # Total published tools available to this user's tier
    tier = getattr(user, "tier", "standard")
    catalogue_q = db.table("mcp_catalogue").select("slug").eq("is_published", True)
    if tier != "super":
        catalogue_q = catalogue_q.eq("tier", "standard")
    total_tools = len(catalogue_q.execute().data or [])

    gateway_url = f"{str(request.base_url).rstrip('/')}/gateway/me"
    devices = _list_devices(user_id)
    active_devices = [d for d in devices if d.get("is_active")]

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
            "active_devices": active_devices,
            "active_nav": "overview",
            "usage_today": usage_today,
            "usage_yesterday": usage_yesterday,
            "usage_month": usage_month,
            "usage_total": usage_total,
            "activity_14days": activity_14days,
            "last_call_time_str": last_call_time_str,
            "last_call_client": last_call_client,
            "total_tools": total_tools,
            "gateway_url": gateway_url,
            "credit_balance": float(user.credit_balance or 0),
            "oauth_expired": bool(oauth_expired),
        }
    )


# ── MCP selection ─────────────────────────────────────────────────────────────

@router.get("/mcps", response_class=HTMLResponse)
async def portal_mcps_get(request: Request, user_id: str = Depends(_require_portal_user)):
    users = _users()
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    # Drop slugs that became inaccessible (unpublished or above tier)
    pruned = users.prune_allowed_mcps(user_id)

    db = get_db()
    query = db.table("mcp_catalogue").select("*").eq("is_published", True).order("name")
    if getattr(user, "tier", "standard") != "super":
        query = query.eq("tier", "standard")
    catalogue = query.execute().data or []
    enabled = set(pruned)

    # Load any saved credential configs for this user
    config_rows = (
        db.table("user_mcp_configs")
        .select("mcp_slug, config")
        .eq("user_id", user_id)
        .execute()
        .data or []
    )
    user_configs: dict = {r["mcp_slug"]: r["config"] for r in config_rows}

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
            "user_configs": user_configs,
        }
    )


@router.post("/mcps/{slug}/config")
async def portal_mcp_config_save(
    slug: str,
    request: Request,
    user_id: str = Depends(_require_portal_user),
):
    import json as _json
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected JSON object"}, status_code=400)

    # Verify slug exists and user can access it
    db = get_db()
    row = db.table("mcp_catalogue").select("slug, config_schema").eq("slug", slug).eq("is_published", True).limit(1).execute()
    if not row.data:
        return JSONResponse({"error": "MCP not found"}, status_code=404)

    # Upsert config
    now = __import__("datetime").datetime.utcnow().isoformat() + "Z"
    db.table("user_mcp_configs").upsert({
        "user_id": user_id,
        "mcp_slug": slug,
        "config": body,
        "updated_at": now,
    }, on_conflict="user_id,mcp_slug").execute()

    try:
        from src.gateway.routes import _invalidate_user_config_cache
        _invalidate_user_config_cache(user_id, slug)
    except Exception:
        pass

    return JSONResponse({"ok": True})


# ── Catalog (browse + add to toolbox) ────────────────────────────────────────

def _catalog_query(db, user):
    q = db.table("mcp_catalogue").select("*").eq("is_published", True).order("name")
    if getattr(user, "tier", "standard") != "super":
        q = q.eq("tier", "standard")
    return q


@router.get("/catalog", response_class=HTMLResponse)
async def portal_catalog_get(
    request: Request,
    user_id: str = Depends(_require_portal_user),
    added: str = "",
):
    users = _users()
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    db = get_db()
    catalogue = _catalog_query(db, user).execute().data or []
    enabled = set(user.allowed_mcp_resources or [])
    available = [m for m in catalogue if m["slug"] not in enabled]
    added_name = ""
    if added:
        match = next((m for m in catalogue if m["slug"] == added), None)
        if match:
            added_name = match.get("name") or added
    client_ctx = {
        "client_id": user_id,
        "client_name": user.display_name or user.email,
        "portal_username": user.email,
    }
    return templates.TemplateResponse(
        request=request, name="portal_catalog.html", context={
            "client": client_ctx,
            "user": user,
            "active_nav": "catalog",
            "available": available,
            "added_name": added_name,
        }
    )


@router.post("/catalog/add", response_class=HTMLResponse)
async def portal_catalog_add(
    request: Request,
    slug: str = Form(...),
    user_id: str = Depends(_require_portal_user),
):
    users = _users()
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    db = get_db()
    catalogue = _catalog_query(db, user).execute().data or []
    valid_slugs = {row["slug"] for row in catalogue}
    if slug not in valid_slugs:
        raise HTTPException(status_code=400, detail="Server not available")
    current = list(user.allowed_mcp_resources or [])
    if slug not in current:
        current.append(slug)
        users.set_allowed_mcps(user_id, current)
    return RedirectResponse(url=f"/portal/catalog?added={slug}", status_code=303)


@router.post("/mcps", response_class=HTMLResponse)
async def portal_mcps_post(request: Request, user_id: str = Depends(_require_portal_user)):
    db = get_db()
    form = await request.form()
    user = _users().get_user(user_id)
    query = db.table("mcp_catalogue").select("slug").eq("is_published", True)
    if user is None or getattr(user, "tier", "standard") != "super":
        query = query.eq("tier", "standard")
    catalogue = query.execute().data or []
    valid_slugs = {row["slug"] for row in catalogue}
    selected = [slug for slug in form.getlist("mcps") if slug in valid_slugs]

    _users().set_allowed_mcps(user_id, selected)
    return RedirectResponse(url="/portal/mcps", status_code=303)


@router.get("/mcps/{slug}/credentials", response_class=HTMLResponse)
async def portal_mcp_credentials_get(
    slug: str, request: Request, user_id: str = Depends(_require_portal_user)
):
    db = get_db()
    mcp_row = (
        db.table("mcp_catalogue")
        .select("slug, name, credentials_schema")
        .eq("slug", slug)
        .eq("is_published", True)
        .limit(1)
        .execute()
    ).data
    if not mcp_row:
        raise HTTPException(status_code=404, detail="MCP not found")
    mcp = mcp_row[0]
    schema: dict = mcp.get("credentials_schema") or {}
    if not schema:
        raise HTTPException(status_code=404, detail="This MCP requires no credentials")

    existing_row = (
        db.table("client_mcp_credentials")
        .select("credentials")
        .eq("user_id", user_id)
        .eq("mcp_slug", slug)
        .limit(1)
        .execute()
    ).data
    existing: dict = (existing_row[0]["credentials"] if existing_row else {}) or {}

    import sys
    print(
        f"CREDS_GET: user_id={user_id!r} slug={slug!r} "
        f"found={bool(existing_row)} keys={list(existing.keys())}",
        file=sys.stderr,
    )

    user = _users().get_user(user_id)
    client_ctx = {
        "client_id": user_id,
        "client_name": getattr(user, "display_name", None) or getattr(user, "email", ""),
        "portal_username": getattr(user, "email", ""),
    }
    return templates.TemplateResponse(
        request=request,
        name="portal_credentials.html",
        context={
            "client": client_ctx,
            "user": user,
            "active_nav": "mcps",
            "mcp": mcp,
            "schema": schema,
            "existing": existing,
        },
    )


@router.post("/mcps/{slug}/credentials", response_class=HTMLResponse)
async def portal_mcp_credentials_post(
    slug: str, request: Request, user_id: str = Depends(_require_portal_user)
):
    db = get_db()
    mcp_row = (
        db.table("mcp_catalogue")
        .select("slug, credentials_schema")
        .eq("slug", slug)
        .eq("is_published", True)
        .limit(1)
        .execute()
    ).data
    if not mcp_row:
        raise HTTPException(status_code=404, detail="MCP not found")
    schema: dict = mcp_row[0].get("credentials_schema") or {}
    if not schema:
        raise HTTPException(status_code=404, detail="This MCP requires no credentials")

    form = await request.form()

    # For password-type fields, a blank submission means "keep existing value".
    existing_row = (
        db.table("client_mcp_credentials")
        .select("credentials")
        .eq("user_id", user_id)
        .eq("mcp_slug", slug)
        .limit(1)
        .execute()
    ).data
    existing: dict = (existing_row[0]["credentials"] if existing_row else {}) or {}

    credentials: dict = {}
    for key, field in schema.items():
        val = form.get(key, "").strip()
        if not val and (field.get("secret") or field.get("type") == "password") and key in existing:
            credentials[key] = existing[key]
        else:
            credentials[key] = val

    db.table("client_mcp_credentials").upsert(
        {"user_id": user_id, "mcp_slug": slug, "credentials": credentials},
        on_conflict="user_id,mcp_slug",
    ).execute()

    return RedirectResponse(url="/portal/mcps", status_code=303)


# ── Setup guide ───────────────────────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def portal_setup(request: Request, user_id: str = Depends(_require_portal_user)):
    user = _users().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    base_url = str(request.base_url).rstrip("/")
    gateway_url = f"{base_url}/gateway/me"
    streamable_url = f"{base_url}/gateway/me/mcp"
    gateway_me_url = f"{base_url}/gateway/me"
    new_secret = request.query_params.get("secret")
    rotated_client_id = request.query_params.get("client_id")
    new_agent_token = request.query_params.get("agent_token")
    devices = _list_devices(user_id)
    agent_tokens = AgentTokenProvider().list_for_user(user_id)

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
            "agent_tokens": agent_tokens,
            "new_agent_token": new_agent_token,
            "active_nav": "setup",
            "gateway_url": gateway_url,
            "streamable_url": streamable_url,
            "gateway_me_url": gateway_me_url,
            "client_id": user_id,
            "new_secret": new_secret,
            "rotated_client_id": rotated_client_id,
        }
    )


@router.post("/setup/agent-tokens/create")
async def portal_agent_token_create(
    user_id: str = Depends(_require_portal_user),
    label: str = Form(...),
):
    raw, _ = AgentTokenProvider().create(user_id=user_id, label=label)
    return RedirectResponse(url=f"/portal/setup?agent_token={raw}", status_code=303)


@router.post("/setup/agent-tokens/revoke")
async def portal_agent_token_revoke(
    user_id: str = Depends(_require_portal_user),
    token_id: str = Form(...),
):
    AgentTokenProvider().revoke(user_id=user_id, token_id=token_id)
    return RedirectResponse(url="/portal/setup", status_code=303)


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
    try:
        from src.gateway.routes import evict_transport
        evict_transport(client_id)
    except Exception:
        pass
    return RedirectResponse(url="/portal/devices", status_code=303)


# ── Devices list page ─────────────────────────────────────────────────────────

@router.get("/devices", response_class=HTMLResponse)
async def portal_devices_get(
    request: Request,
    user_id: str = Depends(_require_portal_user),
):
    user = _users().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    devices = _list_devices(user_id)
    client_ctx = {
        "client_id": user_id,
        "client_name": user.display_name or user.email,
    }
    return templates.TemplateResponse(
        request=request, name="portal_devices.html", context={
            "client": client_ctx,
            "user": user,
            "devices": devices,
            "active_nav": "devices",
        },
    )


@router.post("/devices/revoke")
async def portal_devices_revoke(
    user_id: str = Depends(_require_portal_user),
    client_id: str = Form(...),
):
    """Soft-delete a device — sets is_active=False and revokes its tokens."""
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
    try:
        from src.gateway.routes import evict_transport
        evict_transport(client_id)
    except Exception:
        pass
    return RedirectResponse(url="/portal/devices", status_code=303)


@router.post("/devices/delete")
async def portal_devices_delete(
    user_id: str = Depends(_require_portal_user),
    client_id: str = Form(...),
):
    """Hard-delete a device. Cannot be undone."""
    from src.oauth.provider import SupabaseOAuthProvider
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

    SupabaseOAuthProvider().delete_client(client_id)
    try:
        from src.gateway.routes import evict_transport
        evict_transport(client_id)
    except Exception:
        pass
    return RedirectResponse(url="/portal/devices", status_code=303)


@router.get("/setup/download")
async def portal_setup_download(request: Request, user_id: str = Depends(_require_portal_user)):
    import json
    from fastapi.responses import Response
    user = _users().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    gateway_url = f"{str(request.base_url).rstrip('/')}/gateway/me"
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
            "success": f"Top-up request for {amount:.0f} credits submitted. Admin will review shortly.",
        }
    )


# ── Settings ──────────────────────────────────────────────────────────────────

def _settings_ctx(request, user, *, success: str = "", error: str = ""):
    client_ctx = {
        "client_id": user.user_id,
        "client_name": user.display_name or user.email,
        "credit_balance": user.credit_balance,
    }
    return templates.TemplateResponse(
        request=request, name="portal_settings.html", context={
            "client": client_ctx,
            "user": user,
            "active_nav": "settings",
            "success": success,
            "error": error,
        }
    )


@router.get("/settings", response_class=HTMLResponse)
async def portal_settings_get(
    request: Request,
    user_id: str = Depends(_require_portal_user),
):
    user = _users().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return _settings_ctx(request, user)


@router.post("/settings/email", response_class=HTMLResponse)
async def portal_settings_email(
    request: Request,
    email: str = Form(...),
    current_password: str = Form(...),
    user_id: str = Depends(_require_portal_user),
):
    users = _users()
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    new_email = email.strip().lower()
    if not new_email or "@" not in new_email:
        return _settings_ctx(request, user, error="Enter a valid email address.")
    if not users.verify_password(user, current_password):
        return _settings_ctx(request, user, error="Current password is incorrect.")
    if new_email == (user.email or "").lower():
        return _settings_ctx(request, user, error="That is already your email.")
    existing = users.get_user_by_email(new_email)
    if existing is not None and existing.user_id != user_id:
        return _settings_ctx(request, user, error="That email is already in use.")
    users.update_email(user_id, new_email)
    user = users.get_user(user_id)
    return _settings_ctx(request, user, success="Email updated.")


@router.post("/settings/password", response_class=HTMLResponse)
async def portal_settings_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user_id: str = Depends(_require_portal_user),
):
    users = _users()
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not users.verify_password(user, current_password):
        return _settings_ctx(request, user, error="Current password is incorrect.")
    if len(new_password) < 8:
        return _settings_ctx(request, user, error="New password must be at least 8 characters.")
    if new_password != confirm_password:
        return _settings_ctx(request, user, error="New passwords do not match.")
    if new_password == current_password:
        return _settings_ctx(request, user, error="New password must differ from current.")
    users.set_password(user_id, new_password)
    return _settings_ctx(request, user, success="Password changed.")


# ── Public landing page (root /) ──────────────────────────────────────────────

landing_router = APIRouter()


@landing_router.get("/", response_class=HTMLResponse)
async def public_landing(request: Request):
    """Landing page. Shown to every visitor — authenticated or not."""
    is_authenticated = bool(_verify_session(request.cookies.get(_COOKIE_NAME) or ""))

    from src.portal.landing import (
        get_featured_servers,
        get_landing_stats,
        get_partners,
        get_testimonials,
    )
    from src.admin.settings import get_setting

    featured_servers = get_featured_servers()
    testimonials = get_testimonials()
    partners = get_partners()
    stats = get_landing_stats()
    hero_image_url = get_setting("landing_hero_image_url") or None

    return templates.TemplateResponse(
        request=request, name="portal_landing.html", context={
            "featured_servers": featured_servers,
            "testimonials": testimonials,
            "partners": partners,
            "server_count": stats["server_count"],
            "tool_count": stats["tool_count"],
            "hero_image_url": hero_image_url,
            "is_authenticated": is_authenticated,
        },
    )

"""Social sign-in (Google + Microsoft) for the portal.

Acts as an OAuth 2.0 *client* against the provider, then mints a normal portal
session cookie. Auto-links by email when the provider's verified email matches
an existing user; otherwise creates a new active user with welcome credits and
all published MCPs pre-enabled.

Routes:
  GET /portal/oauth/{provider}/start     — kicks off authorisation
  GET /portal/oauth/{provider}/callback  — handles provider redirect
"""
from __future__ import annotations

import secrets
import sys
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config import get_settings
from src.db import get_db
from src.users.provider import SupabaseUserProvider

router = APIRouter(prefix="/portal/oauth")

_STATE_TTL = 600  # 10 minutes
_STATE_COOKIE = "portal_social_state"


# ── Provider config ──────────────────────────────────────────────────────────

def _provider_config(provider: str) -> dict:
    s = get_settings()
    if provider == "google":
        if not s.GOOGLE_OAUTH_CLIENT_ID or not s.GOOGLE_OAUTH_CLIENT_SECRET:
            raise HTTPException(status_code=503, detail="Google sign-in not configured")
        return {
            "client_id": s.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": s.GOOGLE_OAUTH_CLIENT_SECRET,
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
            "scopes": "openid email profile",
        }
    if provider == "microsoft":
        if not s.MICROSOFT_OAUTH_CLIENT_ID or not s.MICROSOFT_OAUTH_CLIENT_SECRET:
            raise HTTPException(status_code=503, detail="Microsoft sign-in not configured")
        tenant = s.MICROSOFT_OAUTH_TENANT or "common"
        return {
            "client_id": s.MICROSOFT_OAUTH_CLIENT_ID,
            "client_secret": s.MICROSOFT_OAUTH_CLIENT_SECRET,
            "authorize_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
            "token_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            "userinfo_url": "https://graph.microsoft.com/oidc/userinfo",
            "scopes": "openid email profile",
        }
    raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")


def _redirect_uri(provider: str) -> str:
    base = get_settings().OAUTH_ISSUER_URL.rstrip("/")
    return f"{base}/portal/oauth/{provider}/callback"


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().SECRET_KEY, salt="portal-social-state")


# ── /start ────────────────────────────────────────────────────────────────────

@router.get("/{provider}/start")
async def social_start(
    provider: str,
    request: Request,
    next_session: Optional[str] = Query(None),
    next: Optional[str] = Query(None),
):
    cfg = _provider_config(provider)
    nonce = secrets.token_urlsafe(16)
    state = _state_serializer().dumps({
        "n": nonce,
        "ns": next_session or "",
        "np": next or "",
        "p": provider,
    })
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": _redirect_uri(provider),
        "response_type": "code",
        "scope": cfg["scopes"],
        "state": state,
        "access_type": "offline" if provider == "google" else None,
        "prompt": "select_account",
    }
    params = {k: v for k, v in params.items() if v is not None}
    auth_url = f"{cfg['authorize_url']}?{urlencode(params)}"

    secure = get_settings().OAUTH_ISSUER_URL.startswith("https://")
    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        _STATE_COOKIE, nonce,
        httponly=True, samesite="lax", secure=secure, max_age=_STATE_TTL, path="/portal/oauth",
    )
    return response


# ── /callback ─────────────────────────────────────────────────────────────────

@router.get("/{provider}/callback")
async def social_callback(
    provider: str,
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
):
    if error:
        return _login_error(f"Sign-in failed: {error_description or error}")
    if not code or not state:
        return _login_error("Missing authorisation code or state.")

    # Verify state + nonce (CSRF + replay defence).
    try:
        payload = _state_serializer().loads(state, max_age=_STATE_TTL)
    except SignatureExpired:
        return _login_error("Sign-in session expired. Please try again.")
    except BadSignature:
        return _login_error("Invalid sign-in state.")

    cookie_nonce = request.cookies.get(_STATE_COOKIE)
    if not cookie_nonce or cookie_nonce != payload.get("n"):
        return _login_error("Sign-in state mismatch. Please try again.")
    if payload.get("p") != provider:
        return _login_error("Provider mismatch.")

    cfg = _provider_config(provider)

    # Exchange code → tokens.
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                cfg["token_url"],
                data={
                    "code": code,
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                    "redirect_uri": _redirect_uri(provider),
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
        if token_resp.status_code >= 400:
            print(f"SOCIAL: token exchange failed [{provider}] {token_resp.status_code}: {token_resp.text}", file=sys.stderr)
            return _login_error("Could not complete sign-in. Please try again.")
        tokens = token_resp.json()
        access_token = tokens.get("access_token")
        if not access_token:
            return _login_error("Provider returned no access token.")

        async with httpx.AsyncClient(timeout=15.0) as client:
            ui_resp = await client.get(
                cfg["userinfo_url"],
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if ui_resp.status_code >= 400:
            print(f"SOCIAL: userinfo failed [{provider}] {ui_resp.status_code}: {ui_resp.text}", file=sys.stderr)
            return _login_error("Could not read profile from provider.")
        info = ui_resp.json()
    except httpx.HTTPError as exc:
        print(f"SOCIAL: HTTP error [{provider}]: {exc}", file=sys.stderr)
        return _login_error("Network error while contacting provider.")

    sub = str(info.get("sub") or "").strip()
    email = str(info.get("email") or "").strip().lower()
    if not sub or not email:
        return _login_error("Provider did not return a stable identifier and email.")

    # Google sets email_verified bool; Microsoft userinfo treats email as verified.
    if provider == "google" and info.get("email_verified") is False:
        return _login_error("Your Google email is not verified.")

    display_name = (
        info.get("name")
        or " ".join(filter(None, [info.get("given_name"), info.get("family_name")]))
        or None
    )
    avatar_url = info.get("picture")

    next_session = payload.get("ns") or None
    next_path = _safe_next(payload.get("np") or "")

    user = _find_or_create_user(
        provider=provider,
        sub=sub,
        email=email,
        display_name=display_name,
        avatar_url=avatar_url,
    )

    # Mint portal session and resume any pending MCP OAuth session.
    from src.portal.routes import (
        _COOKIE_NAME, _COOKIE_SECURE, _SESSION_MAX_AGE,
        _complete_oauth_session, _sign_session,
    )

    cookie_value = _sign_session(user.user_id)

    if next_session:
        response = _complete_oauth_session(next_session, user.user_id)
        if response is None:
            response = RedirectResponse(url="/portal/?oauth_expired=1", status_code=303)
    else:
        response = RedirectResponse(url=next_path or "/portal/", status_code=303)

    response.set_cookie(
        _COOKIE_NAME, cookie_value,
        httponly=True, samesite="lax", max_age=_SESSION_MAX_AGE, secure=_COOKIE_SECURE,
    )
    response.delete_cookie(_STATE_COOKIE, path="/portal/oauth")
    return response


# ── Helpers ───────────────────────────────────────────────────────────────────

def _login_error(message: str) -> RedirectResponse:
    """Send the user back to the login page with an error banner."""
    from urllib.parse import quote
    return RedirectResponse(url=f"/portal/login?error={quote(message)}", status_code=303)


def _safe_next(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value.startswith("/portal/") and "//" not in value[1:]:
        return value
    return None


def _find_or_create_user(
    *,
    provider: str,
    sub: str,
    email: str,
    display_name: Optional[str],
    avatar_url: Optional[str],
):
    users = SupabaseUserProvider()

    # 1. Existing link by (provider, sub) — fast path.
    linked = users.get_user_by_oauth(provider, sub)
    if linked is not None:
        return linked

    # 2. Email auto-link to existing account.
    existing = users.get_user_by_email(email)
    if existing is not None:
        users.link_oauth(existing.user_id, provider, sub, avatar_url=avatar_url)
        # If the account was unconfirmed (created via registration but never
        # set a password), social sign-in completes verification.
        if not existing.is_active:
            get_db().table("users").update({"is_active": True}).eq(
                "user_id", existing.user_id
            ).execute()
        return existing

    # 3. Fresh user — match register flow defaults.
    db = get_db()
    published = db.table("mcp_catalogue").select("slug").eq("is_published", True).execute()
    allowed_mcps = [r["slug"] for r in (published.data or [])]

    user = users.create_user(
        email=email,
        display_name=display_name,
        credit_balance=5.0,
        allowed_mcp_resources=allowed_mcps,
        is_active=True,
        oauth_provider=provider,
        oauth_sub=sub,
        avatar_url=avatar_url,
    )
    return user

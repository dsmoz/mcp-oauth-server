"""Per-user integration connect/disconnect flows.

Currently implements only Microsoft 365 (Graph API) connection. Unlike
``social.py`` — which uses MS OAuth to *sign in* — this module attaches an MS
identity to an already-authenticated portal user so that mcp-microsoft365 can
act on the user's mailbox/calendar/files via the gateway.

Routes (all require an authenticated portal session):

    GET  /portal/integrations/microsoft365/connect      — kick off consent
    GET  /portal/integrations/microsoft365/callback     — provider redirect
    POST /portal/integrations/microsoft365/disconnect   — revoke
"""

from __future__ import annotations

import secrets
import sys
from typing import Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config import get_settings
from src.integrations.microsoft_graph import (
    SCOPE_STR,
    disconnect as ms_disconnect,
    exchange_code as ms_exchange_code,
)
from src.portal.routes import _require_portal_user
from src.portal.social import _provider_creds

router = APIRouter(prefix="/portal/integrations")

_STATE_TTL = 600
_STATE_COOKIE = "portal_ms_integration_state"


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().SECRET_KEY, salt="portal-ms-integration")


def _redirect_uri() -> str:
    base = get_settings().OAUTH_ISSUER_URL.rstrip("/")
    return f"{base}/portal/integrations/microsoft365/callback"


def _authorize_url(tenant: str, client_id: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": SCOPE_STR,
        "state": state,
        "response_mode": "query",
        "prompt": "consent",  # ensure refresh_token is returned even on re-link
    }
    return (
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?"
        + urlencode(params)
    )


@router.get("/microsoft365/connect")
async def ms_connect_start(
    request: Request,
    user_id: str = Depends(_require_portal_user),
    next: Optional[str] = Query(None),
):
    cid, secret, tenant = _provider_creds("microsoft")
    if not cid or not secret:
        raise HTTPException(status_code=503, detail="Microsoft OAuth not configured")

    nonce = secrets.token_urlsafe(16)
    state = _state_serializer().dumps({"n": nonce, "u": user_id, "np": next or ""})
    auth_url = _authorize_url(tenant or "common", cid, state)

    secure = get_settings().OAUTH_ISSUER_URL.startswith("https://")
    resp = RedirectResponse(url=auth_url, status_code=302)
    resp.set_cookie(
        _STATE_COOKIE, nonce,
        httponly=True, samesite="lax", secure=secure,
        max_age=_STATE_TTL, path="/portal/integrations",
    )
    return resp


@router.get("/microsoft365/callback")
async def ms_connect_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    user_id: str = Depends(_require_portal_user),
):
    if error:
        return _back(f"Microsoft connection failed: {error_description or error}", ok=False)
    if not code or not state:
        return _back("Missing authorisation code or state.", ok=False)

    try:
        payload = _state_serializer().loads(state, max_age=_STATE_TTL)
    except SignatureExpired:
        return _back("Connection session expired. Please try again.", ok=False)
    except BadSignature:
        return _back("Invalid connection state.", ok=False)

    cookie_nonce = request.cookies.get(_STATE_COOKIE)
    if not cookie_nonce or cookie_nonce != payload.get("n"):
        return _back("Connection state mismatch. Please try again.", ok=False)
    if payload.get("u") != user_id:
        return _back("Session/user mismatch — please sign in again.", ok=False)

    try:
        await ms_exchange_code(user_id=user_id, code=code, redirect_uri=_redirect_uri())
    except Exception as exc:
        print(f"INTEGRATION: MS exchange failed user={user_id}: {exc}", file=sys.stderr)
        return _back("Could not complete Microsoft connection. Please try again.", ok=False)

    next_path = _safe_next(payload.get("np") or "") or "/portal/mcps"
    resp = RedirectResponse(url=f"{next_path}?ms365=connected", status_code=303)
    resp.delete_cookie(_STATE_COOKIE, path="/portal/integrations")
    return resp


@router.post("/microsoft365/disconnect")
async def ms_disconnect_post(
    request: Request,
    user_id: str = Depends(_require_portal_user),
):
    ms_disconnect(user_id)
    return RedirectResponse(url="/portal/mcps?ms365=disconnected", status_code=303)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _back(message: str, *, ok: bool) -> RedirectResponse:
    key = "ms365_ok" if ok else "ms365_error"
    return RedirectResponse(
        url=f"/portal/mcps?{key}={quote(message)}",
        status_code=303,
    )


def _safe_next(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value.startswith("/portal/") and "//" not in value[1:]:
        return value
    return None

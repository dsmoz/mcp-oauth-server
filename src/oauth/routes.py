from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.config import get_settings
from src.crypto import verify_secret
from src.oauth.provider import SupabaseOAuthProvider

router = APIRouter()

_TEMPLATES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "admin", "templates")
)
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


def _provider() -> SupabaseOAuthProvider:
    return SupabaseOAuthProvider()


# ── Discovery ────────────────────────────────────────────────────────────────

def _discovery_doc() -> dict:
    settings = get_settings()
    base = settings.OAUTH_ISSUER_URL
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "revocation_endpoint": f"{base}/revoke",
        "registration_endpoint": f"{base}/register",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


@router.get("/.well-known/openid-configuration")
async def openid_configuration():
    return JSONResponse(_discovery_doc())


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    return JSONResponse(_discovery_doc())


# ── Authorization ─────────────────────────────────────────────────────────────

@router.get("/authorize")
async def authorize(
    client_id: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    redirect_uri: Optional[str] = None,
    scope: Optional[str] = None,
    state: Optional[str] = None,
    resource: Optional[str] = None,
):
    if response_type != "code":
        raise HTTPException(status_code=400, detail="unsupported_response_type")
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="unsupported_code_challenge_method")

    provider = _provider()
    client = provider.get_client(client_id)
    if client is None or not client.is_active:
        raise HTTPException(status_code=400, detail="invalid_client")

    # Validate redirect_uri
    if redirect_uri and client.redirect_uris:
        if redirect_uri not in client.redirect_uris:
            raise HTTPException(status_code=400, detail="invalid_redirect_uri")

    scopes = scope.split() if scope else ["mcp"]

    consent_url = provider.authorize(
        client=client,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
        resource=resource,
    )
    return RedirectResponse(url=consent_url, status_code=302)


@router.get("/authorize/consent", response_class=HTMLResponse)
async def consent_get(request: Request, session: str):
    provider = _provider()
    pending = provider.get_pending_session(session)
    if pending is None:
        return HTMLResponse("<h1>Session expired or not found.</h1>", status_code=400)

    client = provider.get_client(pending["client_id"])
    client_name = client.client_name if client else pending["client_id"]
    scopes = pending.get("scopes") or ["mcp"]

    return templates.TemplateResponse(
        request=request,
        name="consent.html",
        context={
            "client_name": client_name,
            "scopes": scopes,
            "session_id": session,
            "error": None,
        },
    )


@router.post("/authorize/consent", response_class=HTMLResponse)
async def consent_post(
    request: Request,
    session_id: str = Form(...),
    password: str = Form(...),
):
    settings = get_settings()
    provider = _provider()

    pending = provider.get_pending_session(session_id)
    if pending is None:
        return HTMLResponse("<h1>Session expired or not found.</h1>", status_code=400)

    client = provider.get_client(pending["client_id"])
    client_name = client.client_name if client else pending["client_id"]
    scopes = pending.get("scopes") or ["mcp"]

    if not secrets.compare_digest(password, settings.ADMIN_PASSWORD):
        return templates.TemplateResponse(
            request=request,
            name="consent.html",
            context={
                "client_name": client_name,
                "scopes": scopes,
                "session_id": session_id,
                "error": "Incorrect password. Please try again.",
            },
            status_code=401,
        )

    try:
        code, redirect_uri = provider.complete_authorization(
            session_id=session_id, client_id=pending["client_id"]
        )
    except ValueError as e:
        return HTMLResponse(f"<h1>Error: {e}</h1>", status_code=400)

    state = pending.get("_state")

    # Build redirect URL
    if not redirect_uri:
        # No redirect URI — show code directly (edge case)
        return HTMLResponse(
            f"<h1>Authorization Code</h1><p>{code}</p>"
            + (f"<p>State: {state}</p>" if state else ""),
            status_code=200,
        )

    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"

    return RedirectResponse(url=location, status_code=302)


# ── Token ─────────────────────────────────────────────────────────────────────

@router.post("/token")
async def token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
):
    provider = _provider()

    # Validate client credentials
    client = provider.get_client(client_id)
    if client is None or not client.is_active:
        raise HTTPException(status_code=401, detail="invalid_client")
    if not verify_secret(client_secret, client.client_secret_hash):
        raise HTTPException(status_code=401, detail="invalid_client")

    if grant_type == "authorization_code":
        if not code or not code_verifier:
            raise HTTPException(status_code=400, detail="missing_parameters")
        try:
            access_token, rt, expires_in = provider.exchange_authorization_code(
                code=code, client_id=client_id, code_verifier=code_verifier
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": expires_in,
                "refresh_token": rt,
            }
        )

    elif grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(status_code=400, detail="missing_parameters")
        try:
            access_token, new_rt, expires_in = provider.exchange_refresh_token(
                refresh_token_str=refresh_token, client_id=client_id
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": expires_in,
                "refresh_token": new_rt,
            }
        )

    else:
        raise HTTPException(status_code=400, detail="unsupported_grant_type")


# ── Revoke ────────────────────────────────────────────────────────────────────

@router.post("/revoke")
async def revoke(
    token: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    provider = _provider()
    client = provider.get_client(client_id)
    if client is None or not client.is_active:
        raise HTTPException(status_code=401, detail="invalid_client")
    if not verify_secret(client_secret, client.client_secret_hash):
        raise HTTPException(status_code=401, detail="invalid_client")

    provider.revoke_token(token)
    return JSONResponse({}, status_code=200)


# ── Introspect (internal) ─────────────────────────────────────────────────────

class IntrospectRequest(BaseModel):
    token: str


@router.post("/introspect")
async def introspect(
    body: IntrospectRequest,
    x_introspect_secret: Optional[str] = Header(None, alias="x-introspect-secret"),
):
    settings = get_settings()
    if not x_introspect_secret or not secrets.compare_digest(
        x_introspect_secret, settings.INTROSPECT_SECRET
    ):
        raise HTTPException(status_code=403, detail="forbidden")

    provider = _provider()
    at = provider.load_access_token(body.token)

    if at is None or at.is_revoked:
        return JSONResponse({"active": False})

    from src.crypto import now_unix
    if at.expires_at and at.expires_at < now_unix():
        return JSONResponse({"active": False})

    return JSONResponse(
        {
            "active": True,
            "client_id": at.client_id,
            "scope": " ".join(at.scopes) if at.scopes else "mcp",
            "exp": at.expires_at,
        }
    )


# ── Register (disabled) ───────────────────────────────────────────────────────

@router.post("/register")
async def register():
    raise HTTPException(
        status_code=405,
        detail="Dynamic client registration is disabled. Use the admin panel at /admin/.",
    )

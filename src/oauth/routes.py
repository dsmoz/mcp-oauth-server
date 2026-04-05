from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.config import get_settings
from src.crypto import compute_dcr_fingerprint, generate_client_id, generate_token, hash_secret, now_unix, verify_secret
from src.db import get_db
from src import email as em
from src.limiter import limiter
from src.oauth.provider import SupabaseOAuthProvider
from src import telegram as tg

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
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
    }


@router.get("/.well-known/openid-configuration")
async def openid_configuration():
    return JSONResponse(_discovery_doc())


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    return JSONResponse(_discovery_doc())


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def oauth_protected_resource(request: Request):
    """RFC 9728 — advertise the authorization server for this resource."""
    base = get_settings().OAUTH_ISSUER_URL
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })


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

    # Validate redirect_uri — always allow localhost for native clients (MCP spec)
    if redirect_uri and client.redirect_uris:
        is_localhost = redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1")
        if not is_localhost and redirect_uri not in client.redirect_uris:
            raise HTTPException(status_code=400, detail="invalid_redirect_uri")

    scopes = ["mcp"]

    session_id = provider.authorize(
        client=client,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
        resource=resource,
    )
    return RedirectResponse(url=f"/portal/login?next_session={session_id}", status_code=302)


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram webhook updates (registration notifications only)."""
    settings = get_settings()
    if settings.TELEGRAM_WEBHOOK_SECRET:
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming != settings.TELEGRAM_WEBHOOK_SECRET:
            return JSONResponse({"ok": False}, status_code=403)
    return JSONResponse({"ok": True})


# ── Token ─────────────────────────────────────────────────────────────────────

@router.post("/token")
async def token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: Optional[str] = Form(None),
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
    # PKCE flows (public clients) don't send a client_secret — code_verifier is the proof
    if not code_verifier and not verify_secret(client_secret or "", client.client_secret_hash):
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

    # Log usage — fire and forget, never block the response
    try:
        get_db().table("oauth_usage_logs").insert({"client_id": at.client_id}).execute()
    except Exception:
        pass

    return JSONResponse(
        {
            "active": True,
            "client_id": at.client_id,
            "scope": " ".join(at.scopes) if at.scopes else "mcp",
            "exp": at.expires_at,
        }
    )


# ── Self-service registration ─────────────────────────────────────────────────

def _make_captcha() -> tuple[str, str]:
    """Return (question_text, signed_answer) for a simple arithmetic CAPTCHA."""
    import random
    from itsdangerous import URLSafeSerializer
    a, b = random.randint(2, 12), random.randint(2, 12)
    question = f"What is {a} + {b}?"
    answer = str(a + b)
    signed = URLSafeSerializer(get_settings().SECRET_KEY, salt="captcha").dumps(answer)
    return question, signed


def _verify_captcha(user_answer: str, signed_answer: str) -> bool:
    from itsdangerous import URLSafeSerializer, BadSignature
    try:
        expected = URLSafeSerializer(get_settings().SECRET_KEY, salt="captcha").loads(signed_answer)
        return user_answer.strip() == expected
    except BadSignature:
        return False


@router.get("/register", response_class=HTMLResponse)
async def register_get(request: Request):
    question, signed = _make_captcha()
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"error": None, "captcha_question": question, "captcha_signed": signed},
    )


@router.post("/register/submit", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def register_submit(
    request: Request,
    company_name: str = Form(...),
    contact_name: str = Form(...),
    contact_email: str = Form(...),
    use_case: str = Form(...),
    redirect_uris_raw: str = Form(""),
    website: str = Form(""),
    form_loaded_at: str = Form(""),
    captcha_answer: str = Form(""),
    captcha_signed: str = Form(""),
):
    import sys

    # Anti-bot: honeypot field must be empty
    if website:
        return RedirectResponse(url="/register/success", status_code=303)

    # Anti-bot: form must have been visible for at least 3 seconds
    import time as _time
    try:
        elapsed_ms = _time.time() * 1000 - float(form_loaded_at)
        if elapsed_ms < 3000:
            return RedirectResponse(url="/register/success", status_code=303)
    except (ValueError, TypeError):
        pass

    # Anti-bot: math CAPTCHA
    if not _verify_captcha(captcha_answer, captcha_signed):
        new_q, new_signed = _make_captcha()
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "Incorrect answer to the security question. Please try again.",
                "captcha_question": new_q,
                "captcha_signed": new_signed,
                "company_name": company_name,
                "contact_name": contact_name,
                "contact_email": contact_email,
                "use_case": use_case,
                "redirect_uris_raw": redirect_uris_raw,
            },
            status_code=422,
        )

    settings = get_settings()
    db = get_db()

    # Parse redirect URIs
    redirect_uris = [u.strip() for u in redirect_uris_raw.splitlines() if u.strip()]

    # Create OAuth client immediately (no admin approval gate)
    client_id = generate_client_id()
    raw_secret = generate_token(32)
    secret_hash = hash_secret(raw_secret)

    # Pre-populate toolbox with all published MCPs
    published = db.table("mcp_catalogue").select("slug").eq("is_published", True).execute()
    allowed_mcps = [r["slug"] for r in (published.data or [])]

    db.table("oauth_clients").insert({
        "client_id": client_id,
        "client_secret_hash": secret_hash,
        "client_name": company_name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "scope": "mcp",
        "allowed_mcp_resources": allowed_mcps,
        "created_by": contact_email,
        "is_active": False,  # activated when user completes setup-password
        "portal_username": contact_email,
        "credit_balance": 0,
    }).execute()

    # Log registration request for admin visibility (status=approved immediately)
    reg_result = db.table("oauth_registration_requests").insert({
        "company_name": company_name,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "use_case": use_case,
        "redirect_uris_raw": redirect_uris_raw.strip(),
        "status": "approved",
        "reviewed_at": __import__("datetime").datetime.utcnow().isoformat(),
        "reviewed_by": "self-service",
    }).execute()
    request_id = reg_result.data[0]["id"] if reg_result.data else "unknown"

    # Generate portal setup token (one-time 24h link to set password)
    from src.portal.routes import create_setup_token
    setup_token = create_setup_token(client_id)

    # Send credentials email
    try:
        await em.send_approval_email(
            contact_name=contact_name,
            contact_email=contact_email,
            company_name=company_name,
            client_id=client_id,
            issuer_url=settings.OAUTH_ISSUER_URL,
            setup_token=setup_token,
        )
    except Exception as exc:
        print(f"WARNING: credentials email failed: {exc}", file=sys.stderr)

    # Notify owner via Telegram (informational only — no approval needed)
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_OWNER_CHAT_ID:
        try:
            await tg.send_registration_alert(
                company_name=company_name,
                contact_name=contact_name,
                contact_email=contact_email,
            )
        except Exception as exc:
            print(f"WARNING: Telegram registration alert failed: {exc}", file=sys.stderr)

    return RedirectResponse(url="/register/success", status_code=303)


@router.get("/register/success", response_class=HTMLResponse)
async def register_success(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="register_success.html",
        context={},
    )


@router.post("/register")
async def dynamic_client_registration(request: Request):
    """RFC 7591 Dynamic Client Registration for MCP clients (Claude Desktop, mcp-remote, etc.)."""
    import sys
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Invalid JSON body"},
            status_code=400,
        )

    client_name = body.get("client_name", "MCP Client")
    redirect_uris = body.get("redirect_uris", [])
    grant_types = body.get("grant_types", ["authorization_code"])
    scope = body.get("scope", "mcp")

    db = get_db()
    fingerprint = compute_dcr_fingerprint(client_name, redirect_uris)

    # --- Dedup: return existing client if fingerprint matches ---
    existing = None
    if fingerprint:
        result = (
            db.table("oauth_clients")
            .select("client_id, created_at")
            .eq("dcr_fingerprint", fingerprint)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        existing = result.data[0] if result.data else None

    if existing:
        # Client already registered — return client_id without rotating the secret.
        # The caller must use the secret from its original registration.
        print(f"DCR: dedup hit — returning {existing['client_id']} ({client_name}), secret unchanged", file=sys.stderr)

        response_body = {
            **body,
            "client_id": existing["client_id"],
            "client_id_issued_at": int(
                __import__("datetime").datetime.fromisoformat(
                    existing["created_at"].replace("Z", "+00:00")
                ).timestamp()
            ) if existing.get("created_at") else now_unix(),
            "client_secret_expires_at": 0,
        }
        return JSONResponse(response_body, status_code=200)

    # --- New client registration ---
    client_id = generate_client_id()
    raw_secret = generate_token(32)
    secret_hash = hash_secret(raw_secret)

    # Pre-populate toolbox with all published MCPs
    published = db.table("mcp_catalogue").select("slug").eq("is_published", True).execute()
    allowed_mcps = [r["slug"] for r in (published.data or [])]

    try:
        db.table("oauth_clients").insert({
            "client_id": client_id,
            "client_secret_hash": secret_hash,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "scope": scope if isinstance(scope, str) else " ".join(scope),
            "allowed_mcp_resources": allowed_mcps,
            "created_by": f"dynamic:{client_name}",
            "is_active": True,
            "credit_balance": 0,
            "dcr_fingerprint": fingerprint,
        }).execute()
    except Exception as exc:
        # Race condition: another request inserted the same fingerprint concurrently
        if fingerprint and "uq_dcr_fingerprint" in str(exc):
            result = (
                db.table("oauth_clients")
                .select("client_id, created_at")
                .eq("dcr_fingerprint", fingerprint)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
            if result.data:
                winner = result.data[0]
                response_body = {
                    **body,
                    "client_id": winner["client_id"],
                    "client_id_issued_at": now_unix(),
                    "client_secret_expires_at": 0,
                }
                return JSONResponse(response_body, status_code=200)
        raise

    print(f"DCR: registered {client_id} ({client_name})", file=sys.stderr)

    # RFC 7591 response — merge client metadata with issued credentials
    response_body = {
        **body,
        "client_id": client_id,
        "client_secret": raw_secret,
        "client_id_issued_at": now_unix(),
        "client_secret_expires_at": 0,  # does not expire
    }
    return JSONResponse(response_body, status_code=201)

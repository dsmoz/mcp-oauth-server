from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.config import get_settings
from src.crypto import generate_client_id, generate_token, hash_secret, now_unix, verify_secret
from src.db import get_db
from src import email as em
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
    settings = get_settings()
    provider = _provider()
    pending = provider.get_pending_session(session)
    if pending is None:
        return HTMLResponse("<h1>Session expired or not found.</h1>", status_code=400)

    client = provider.get_client(pending["client_id"])
    client_name = client.client_name if client else pending["client_id"]
    scopes = pending.get("scopes") or ["mcp"]

    # Fallback: if Telegram is not configured, use the password form
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_OWNER_CHAT_ID:
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

    # Check if we already sent a Telegram message for this session (duplicate guard)
    import json as _json
    try:
        session_data = _json.loads(pending.get("resource") or "{}")
    except (ValueError, TypeError):
        session_data = {}

    if not session_data.get("telegram_message_id"):
        try:
            message_id = await tg.send_approval_request(
                session_id=session,
                client_name=client_name,
                scopes=scopes,
            )
            provider.update_session_telegram_id(session, message_id)
        except Exception as exc:
            import sys
            print(f"WARNING: Telegram send failed: {exc}", file=sys.stderr)

    return templates.TemplateResponse(
        request=request,
        name="consent_waiting.html",
        context={
            "client_name": client_name,
            "session_id": session,
        },
    )


@router.post("/authorize/consent", response_class=HTMLResponse)
async def consent_post(
    request: Request,
    session_id: str = Form(...),
    password: str = Form(...),
):
    """Fallback password-based consent (used when Telegram is not configured)."""
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

    if not redirect_uri:
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


@router.get("/consent/status")
async def consent_status(session: str):
    """Polled by the waiting page every 2 seconds to check approval state."""
    provider = _provider()

    # Check if already approved (in-process store)
    approved = provider.get_completed_code_for_session(session)
    if approved:
        redirect_uri = approved.get("redirect_uri")
        code = approved["code"]
        state = approved.get("state")

        if not redirect_uri:
            return JSONResponse({"status": "approved", "redirect": None})

        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={code}"
        if state:
            location += f"&state={state}"
        return JSONResponse({"status": "approved", "redirect": location})

    # Check if session still pending
    pending = provider.get_pending_session(session)
    if pending is None:
        # Row is gone and not in approved store = denied or expired
        return JSONResponse({"status": "denied"})

    return JSONResponse({"status": "pending"})


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram inline button callbacks."""
    body = await request.json()
    callback = body.get("callback_query", {})
    data = callback.get("data", "")
    callback_id = callback.get("id")

    if not data or not callback_id:
        return JSONResponse({"ok": True})

    try:
        action, session_id = data.split(":", 1)
    except ValueError:
        return JSONResponse({"ok": True})

    provider = _provider()

    # Get message_id — prefer stored value in session, fall back to Telegram callback body
    import json as _json
    message_id: Optional[int] = callback.get("message", {}).get("message_id")
    row = provider._single("oauth_authorization_codes", code=session_id)
    if row:
        try:
            session_data = _json.loads(row.get("resource") or "{}")
            message_id = session_data.get("telegram_message_id") or message_id
        except (ValueError, TypeError):
            pass

    if action == "approve":
        try:
            # Get state from pending session before completing (complete_authorization wipes session data)
            pending = provider.get_pending_session(session_id)
            state = pending.get("_state") if pending else None

            code, redirect_uri = provider.mark_session_approved(session_id)

            # Store for the waiting page to pick up
            provider.store_approved_redirect(
                session_id=session_id,
                code=code,
                redirect_uri=redirect_uri,
                state=state,
            )

            await tg.answer_callback(callback_id, "✅ Access granted")
            if message_id:
                await tg.edit_message_result(message_id, "✅ *Access granted*")
        except Exception as exc:
            import sys
            print(f"WARNING: Telegram approval failed: {exc}", file=sys.stderr)
            await tg.answer_callback(callback_id, "⚠️ Session expired or already processed")

    elif action == "deny":
        provider.mark_session_denied(session_id)
        await tg.answer_callback(callback_id, "❌ Access denied")
        if message_id:
            await tg.edit_message_result(message_id, "❌ *Access denied*")

    elif action == "reg_approve":
        request_id = session_id
        db = get_db()
        result = db.table("oauth_registration_requests").select("*").eq("id", request_id).execute()
        reg = result.data[0] if result.data else None
        if reg is None or reg["status"] != "pending":
            await tg.answer_callback(callback_id, "⚠️ Not found or already processed")
        else:
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
                "portal_username": reg["contact_email"],
            }).execute()
            db.table("oauth_registration_requests").update({
                "status": "approved",
                "reviewed_at": "now()",
                "reviewed_by": "telegram",
            }).eq("id", request_id).execute()
            from src.portal.routes import create_setup_token
            setup_token = create_setup_token(client_id)
            try:
                await em.send_approval_email(
                    contact_name=reg.get("contact_name", reg["contact_email"]),
                    contact_email=reg["contact_email"],
                    company_name=reg["company_name"],
                    client_id=client_id,
                    raw_secret=raw_secret,
                    issuer_url=get_settings().OAUTH_ISSUER_URL,
                    setup_token=setup_token,
                )
            except Exception as exc:
                import sys
                print(f"WARNING: approval email failed: {exc}", file=sys.stderr)
            await tg.answer_callback(callback_id, "✅ Registration approved")
            await tg.edit_message_result(
                message_id,
                f"✅ *Approved*\nClient ID: `{client_id}`",
            )

    elif action == "reg_reject":
        request_id = session_id
        db = get_db()
        result = db.table("oauth_registration_requests").select("status").eq("id", request_id).execute()
        reg = result.data[0] if result.data else None
        if reg is None or reg["status"] != "pending":
            await tg.answer_callback(callback_id, "⚠️ Not found or already processed")
        else:
            db.table("oauth_registration_requests").delete().eq("id", request_id).execute()
            await tg.answer_callback(callback_id, "❌ Registration rejected")
            await tg.edit_message_result(message_id, "❌ *Registration rejected*")

    return JSONResponse({"ok": True})


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

@router.get("/register", response_class=HTMLResponse)
async def register_get(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"error": None},
    )


@router.post("/register/submit", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    company_name: str = Form(...),
    contact_name: str = Form(...),
    contact_email: str = Form(...),
    use_case: str = Form(...),
    redirect_uris_raw: str = Form(""),
    website: str = Form(""),
    form_loaded_at: str = Form(""),
):
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

    from src.db import get_db
    db = get_db()
    result = db.table("oauth_registration_requests").insert({
        "company_name": company_name,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "use_case": use_case,
        "redirect_uris_raw": redirect_uris_raw.strip(),
        "status": "pending",
    }).execute()

    request_id = result.data[0]["id"] if result.data else "unknown"

    # Notify owner via Telegram (non-blocking — failure must not block the user)
    settings = get_settings()
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_OWNER_CHAT_ID:
        try:
            await tg.send_registration_alert(
                request_id=request_id,
                company_name=company_name,
                contact_name=contact_name,
                contact_email=contact_email,
            )
        except Exception as exc:
            import sys
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
async def register():
    raise HTTPException(
        status_code=405,
        detail="Dynamic client registration is disabled. Use /register to submit a registration request.",
    )

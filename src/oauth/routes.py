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
from src.users.provider import SupabaseUserProvider
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
        "revocation_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "resource_indicators_supported": True,
        "logo_uri": "https://media.dsmozconsultancy.com/logos/dsmoz-connect.png",
        "service_name": "DS-MOZ Connect",
    }


_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate"}
# Discovery docs are spec-stable across deployments — let clients and any
# intermediary cache them hard.
_DISCOVERY_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}


@router.get("/.well-known/openid-configuration")
async def openid_configuration():
    return JSONResponse(_discovery_doc(), headers=_DISCOVERY_CACHE_HEADERS)


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    return JSONResponse(_discovery_doc(), headers=_DISCOVERY_CACHE_HEADERS)


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def oauth_protected_resource(request: Request, path: str = ""):
    """RFC 9728 — advertise the authorization server for this resource.

    The `resource` field MUST reflect the specific protected resource URI
    that the metadata describes, including any path component. Strict
    clients (e.g. Claude.ai) reject tokens whose resource indicator does
    not match the path-specific resource URI.
    """
    base = get_settings().OAUTH_ISSUER_URL.rstrip("/")
    resource = f"{base}/{path}" if path else base
    return JSONResponse(
        {
            "resource": resource,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp"],
            "logo_uri": "https://media.dsmozconsultancy.com/logos/dsmoz-connect.png",
            "service_name": "DS-MOZ Connect",
        },
        headers=_DISCOVERY_CACHE_HEADERS,
    )


# ── Authorization ─────────────────────────────────────────────────────────────

@router.get("/authorize")
async def authorize(
    client_id: str,
    response_type: str,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    scope: Optional[str] = None,
    state: Optional[str] = None,
    resource: Optional[str] = None,
):
    if response_type != "code":
        raise HTTPException(status_code=400, detail="unsupported_response_type")
    normalized_code_challenge_method: Optional[str] = None
    if code_challenge:
        normalized_code_challenge_method = code_challenge_method or "S256"
        if normalized_code_challenge_method != "S256":
            raise HTTPException(status_code=400, detail="unsupported_code_challenge_method")
    elif code_challenge_method is not None:
        raise HTTPException(status_code=400, detail="invalid_request")

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
        code_challenge_method=normalized_code_challenge_method,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
        resource=resource,
    )
    return RedirectResponse(url=f"/portal/login?next_session={session_id}", status_code=302)


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram webhook updates."""
    import sys
    client_host = request.client.host if request.client else "?"
    print(f"[telegram_webhook] hit from {client_host}", file=sys.stderr, flush=True)
    try:
        import sentry_sdk
        sentry_sdk.capture_message(f"telegram_webhook hit from {client_host}", level="info")
    except Exception:
        pass

    expected_secret = tg._webhook_secret()
    incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if expected_secret:
        if incoming != expected_secret:
            print(
                f"[telegram_webhook] 403 secret mismatch — incoming_len={len(incoming)} expected_len={len(expected_secret)}",
                file=sys.stderr, flush=True,
            )
            try:
                import sentry_sdk
                sentry_sdk.capture_message(
                    f"telegram_webhook: secret mismatch (incoming_len={len(incoming)}, expected_len={len(expected_secret)})",
                    level="warning",
                )
            except Exception:
                pass
            return JSONResponse({"ok": False, "reason": "secret mismatch"}, status_code=403)
    else:
        print("[telegram_webhook] WARNING: no expected_secret configured", file=sys.stderr, flush=True)

    try:
        body = await request.json()
    except Exception as exc:
        print(f"[telegram_webhook] body parse failed: {exc}", file=sys.stderr, flush=True)
        try:
            import sentry_sdk; sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        return JSONResponse({"ok": False, "reason": "bad json"}, status_code=400)

    print(f"[telegram_webhook] body keys: {list(body.keys())}", file=sys.stderr, flush=True)
    try:
        import sentry_sdk
        sentry_sdk.capture_message(
            f"telegram_webhook body keys={list(body.keys())} cq_data={body.get('callback_query', {}).get('data')!r}",
            level="info",
        )
    except Exception:
        pass

    cq = body.get("callback_query")
    if cq:
        try:
            await _handle_topup_callback(cq)
        except Exception as exc:
            print(f"[telegram_webhook] callback handler raised: {exc}", file=sys.stderr, flush=True)
            try:
                import sentry_sdk; sentry_sdk.capture_exception(exc)
            except Exception:
                pass
    return JSONResponse({"ok": True})


async def _handle_topup_callback(cq: dict) -> None:
    import sys
    data = cq.get("data", "")
    cq_id = cq["id"]
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    print(f"[telegram_callback] data={data!r} chat_id={chat_id}", file=sys.stderr, flush=True)

    if data.startswith("topup_approve:"):
        request_id = data.split(":", 1)[1]
        result = _do_approve_topup(request_id)
        print(f"[telegram_callback] approve {request_id} -> {result}", file=sys.stderr, flush=True)
        if result == "approved":
            await tg.answer_callback_query(cq_id, "✅ Approved — credits added")
            await tg.edit_topup_message(chat_id, message_id, "✅ *Approved* — credits added.")
        elif result == "already_done":
            await tg.answer_callback_query(cq_id, "Already processed")
        else:
            await tg.answer_callback_query(cq_id, "❌ Error — check admin panel")

    elif data.startswith("topup_reject:"):
        request_id = data.split(":", 1)[1]
        result = _do_reject_topup(request_id)
        print(f"[telegram_callback] reject {request_id} -> {result}", file=sys.stderr, flush=True)
        if result == "rejected":
            await tg.answer_callback_query(cq_id, "❌ Rejected")
            await tg.edit_topup_message(chat_id, message_id, "❌ *Rejected*.")
        elif result == "already_done":
            await tg.answer_callback_query(cq_id, "Already processed")
        else:
            await tg.answer_callback_query(cq_id, "Error")
    else:
        print(f"[telegram_callback] unknown data prefix: {data!r}", file=sys.stderr, flush=True)


def _do_approve_topup(request_id: str) -> str:
    """Add credits and mark approved. Returns 'approved', 'already_done', or 'error'."""
    import datetime
    try:
        db = get_db()
        row_res = db.table("credit_topup_requests").select("*").eq("id", request_id).limit(1).execute()
        if not row_res.data:
            return "error"
        row = row_res.data[0]
        if row["status"] != "pending":
            return "already_done"
        user_id = row["user_id"]
        amount = float(row["amount"])
        user_res = db.table("users").select("credit_balance").eq("user_id", user_id).limit(1).execute()
        if not user_res.data:
            return "error"
        current = float(user_res.data[0].get("credit_balance") or 0)
        db.table("users").update({"credit_balance": current + amount}).eq("user_id", user_id).execute()
        db.table("credit_topup_requests").update({
            "status": "approved",
            "reviewed_at": datetime.datetime.utcnow().isoformat(),
            "reviewed_by": "telegram",
        }).eq("id", request_id).execute()
        return "approved"
    except Exception as exc:
        import sys, traceback
        print(f"ERROR: topup approve failed for {request_id}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        try:
            import sentry_sdk; sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        return "error"


def _do_reject_topup(request_id: str) -> str:
    """Mark rejected. Returns 'rejected', 'already_done', or 'error'."""
    import datetime
    try:
        db = get_db()
        row_res = db.table("credit_topup_requests").select("status").eq("id", request_id).limit(1).execute()
        if not row_res.data:
            return "error"
        if row_res.data[0]["status"] != "pending":
            return "already_done"
        db.table("credit_topup_requests").update({
            "status": "rejected",
            "reviewed_at": datetime.datetime.utcnow().isoformat(),
            "reviewed_by": "telegram",
        }).eq("id", request_id).execute()
        return "rejected"
    except Exception as exc:
        import sys, traceback
        print(f"ERROR: topup reject failed for {request_id}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        try:
            import sentry_sdk; sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        return "error"


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
    resource: Optional[str] = Form(None),  # RFC 8707 — accepted, optional
    scope: Optional[str] = Form(None),
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
        if not code:
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
                "token_type": "Bearer",
                "expires_in": expires_in,
                "refresh_token": rt,
                "scope": "mcp",
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
                "token_type": "Bearer",
                "expires_in": expires_in,
                "refresh_token": new_rt,
                "scope": "mcp",
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
    cost: float | None = None        # credits to deduct atomically; None = no deduction
    upstream: str | None = None      # identifier of caller, e.g. "linguist", for usage log
    units: int | None = None         # optional metric (e.g. char count) — logged only


def _deduct_or_reject(user_id: str, amount: float) -> tuple[str, float | None]:
    """Atomically deduct credits via Supabase RPC.

    Mirrors :func:`src.gateway.routes._deduct_credits` (same ``deduct_credits_user``
    RPC) but returns a richer status so the introspect endpoint can distinguish
    "user is broke" from "billing system is down" — only the former is a clean
    business decision; the latter should never be reported as success.

    Args:
        user_id: The user owning the token being introspected.
        amount: Positive number of credits to deduct.

    Returns:
        Tuple of ``(status, new_balance)`` where ``status`` is one of:

        * ``"ok"`` — deduction succeeded; ``new_balance`` is the post-deduction balance.
        * ``"insufficient"`` — RPC returned -1 (user lacks credit); ``new_balance`` is ``None``.
        * ``"error"`` — RPC raised; ``new_balance`` is ``None``. Treat as a hard fail.

    Example:
        >>> status, balance = _deduct_or_reject("user-123", 0.5)
        >>> if status == "ok":
        ...     print(f"Remaining: {balance}")

    Raises:
        Never raises — RPC exceptions are caught and reported via the
        ``"error"`` status so callers can fail closed without try/except.
    """
    try:
        result = get_db().rpc(
            "deduct_credits_user", {"p_user_id": user_id, "p_amount": amount}
        ).execute()
        new_balance = float(result.data) if result.data is not None else -1.0
    except Exception as exc:
        import sys
        print(
            f"WARNING: credit deduction failed for user {user_id}: {exc}",
            file=sys.stderr,
        )
        return "error", None
    if new_balance < 0:
        return "insufficient", None
    return "ok", new_balance


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

    # Optional atomic credit deduction. When ``cost`` is None or <= 0 we keep
    # the legacy behaviour (no deduction, no ``credits_remaining`` field) so
    # existing callers — notably the MCP gateway — are unaffected.
    credits_remaining: float | None = None
    if body.cost is not None and body.cost > 0:
        status, new_balance = _deduct_or_reject(at.user_id, body.cost)
        if status == "insufficient":
            return JSONResponse(
                {"active": False, "reason": "insufficient_credits"},
                status_code=200,
            )
        if status == "error":
            # Fail closed on billing system failure — never grant a free call.
            return JSONResponse(
                {"active": False, "reason": "billing_error"},
                status_code=200,
            )
        credits_remaining = new_balance

    # Log usage — fire and forget, never block the response. We reuse the
    # oauth_usage_logs schema the gateway already writes to.
    try:
        log_row: dict = {
            "client_id": at.client_id,
            "user_id": at.user_id,
            "credits_used": body.cost or 0,
        }
        if body.upstream:
            log_row["endpoint"] = f"introspect/{body.upstream}"
        get_db().table("oauth_usage_logs").insert(log_row).execute()
    except Exception:
        pass

    user_is_admin = False
    try:
        user_row = get_db().table("users").select("is_admin").eq("user_id", at.user_id).limit(1).execute()
        if user_row.data:
            user_is_admin = bool(user_row.data[0].get("is_admin", False))
    except Exception:
        pass

    response: dict = {
        "active": True,
        "client_id": at.client_id,
        "user_id": at.user_id,
        "scope": " ".join(at.scopes) if at.scopes else "mcp",
        "exp": at.expires_at,
        "is_admin": user_is_admin,
    }
    if credits_remaining is not None:
        response["credits_remaining"] = credits_remaining
    return JSONResponse(response)


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
            },
            status_code=422,
        )

    settings = get_settings()
    db = get_db()
    users = SupabaseUserProvider()

    # Pre-populate toolbox with all published MCPs
    published = db.table("mcp_catalogue").select("slug").eq("is_published", True).execute()
    allowed_mcps = [r["slug"] for r in (published.data or [])]

    # Create user row (the tenant). Inactive until password is set via email link.
    existing = users.get_user_by_email(contact_email)
    if existing is not None and not existing.is_active:
        # Retry path: unconfirmed user — mint fresh setup token + resend email.
        from src.portal.routes import create_setup_token
        setup_token = create_setup_token(existing.user_id)
        try:
            await em.send_approval_email(
                contact_name=contact_name,
                contact_email=contact_email,
                company_name=company_name,
                user_id=existing.user_id,
                issuer_url=settings.OAUTH_ISSUER_URL,
                setup_token=setup_token,
            )
        except Exception as exc:
            print(f"WARNING: retry credentials email failed: {exc}", file=sys.stderr)
        return RedirectResponse(url="/register/success", status_code=303)
    if existing is not None:
        new_q, new_signed = _make_captcha()
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "An account with this email already exists. Please sign in via the portal.",
                "captcha_question": new_q,
                "captcha_signed": new_signed,
                "company_name": company_name,
                "contact_name": contact_name,
                "contact_email": contact_email,
            },
            status_code=409,
        )
    try:
        user = users.create_user(
            email=contact_email,
            display_name=contact_name,
            credit_balance=5.0,
            allowed_mcp_resources=allowed_mcps,
            is_active=False,
        )
    except ValueError:
        return RedirectResponse(url="/register/success", status_code=303)

    # Create an OAuth client bound to the user (claimed on creation).
    client_id = generate_client_id()
    raw_secret = generate_token(32)
    secret_hash = hash_secret(raw_secret)
    from datetime import datetime, timezone
    claimed_at = datetime.now(timezone.utc).isoformat()

    db.table("oauth_clients").insert({
        "client_id": client_id,
        "client_secret_hash": secret_hash,
        "client_name": company_name,
        "redirect_uris": [],
        "grant_types": ["authorization_code"],
        "scope": "mcp",
        "created_by": contact_email,
        "is_active": False,  # activated once the user completes setup-password
        "user_id": user.user_id,
        "claimed_at": claimed_at,
    }).execute()

    # Log registration request for admin visibility (status=approved immediately)
    reg_result = db.table("oauth_registration_requests").insert({
        "company_name": company_name,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "use_case": "",
        "redirect_uris_raw": "",
        "status": "approved",
        "reviewed_at": __import__("datetime").datetime.utcnow().isoformat(),
        "reviewed_by": "self-service",
    }).execute()
    request_id = reg_result.data[0]["id"] if reg_result.data else "unknown"

    # Generate portal setup token (one-time 24h link to set password). Keyed on user_id.
    from src.portal.routes import create_setup_token
    setup_token = create_setup_token(user.user_id)

    # Send credentials email
    try:
        await em.send_approval_email(
            contact_name=contact_name,
            contact_email=contact_email,
            company_name=company_name,
            user_id=user.user_id,
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

    # --- Dedup: return existing UNCLAIMED client if fingerprint matches ---
    # Claimed clients are per-user devices. A new DCR call for the same fingerprint
    # from a different device/session must mint a fresh unclaimed client so the
    # next logged-in user can claim it, instead of hijacking someone else's device.
    existing = None
    if fingerprint:
        result = (
            db.table("oauth_clients")
            .select("client_id, created_at")
            .eq("dcr_fingerprint", fingerprint)
            .eq("is_active", True)
            .is_("user_id", "null")
            .limit(1)
            .execute()
        )
        existing = result.data[0] if result.data else None

    if existing:
        # Unclaimed client already pending — return it so the same session can resume.
        print(f"DCR: dedup hit — returning unclaimed {existing['client_id']} ({client_name})", file=sys.stderr)

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

    # --- New unclaimed client registration ---
    # user_id is NULL; populated atomically when the first authorising user logs in.
    # Credit balance and allowed_mcp_resources live on the user row, not the client.
    client_id = generate_client_id()
    raw_secret = generate_token(32)
    secret_hash = hash_secret(raw_secret)

    try:
        db.table("oauth_clients").insert({
            "client_id": client_id,
            "client_secret_hash": secret_hash,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "scope": scope if isinstance(scope, str) else " ".join(scope),
            "created_by": f"dynamic:{client_name}",
            "is_active": True,
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
                .is_("user_id", "null")
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

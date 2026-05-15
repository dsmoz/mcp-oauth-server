"""
Telegram Bot API wrapper for registration notifications.
Uses plain httpx — no telegram library dependency.

Settings precedence: admin_settings table (managed via /admin/settings) overrides
environment variables. Empty DB row → fall back to env.
"""
from __future__ import annotations

import httpx

from src.config import get_settings

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _resolve(db_key: str, env_value: str) -> str:
    """Prefer DB-managed admin setting over env value. Empty DB → env fallback."""
    try:
        from src.admin.settings import get_setting
        v = get_setting(db_key)
        if v:
            return v
    except Exception:
        pass
    return env_value or ""


def _bot_token() -> str:
    return _resolve("telegram_bot_token", get_settings().TELEGRAM_BOT_TOKEN)


def _chat_id() -> str:
    return _resolve("telegram_chat_id", get_settings().TELEGRAM_OWNER_CHAT_ID)


def _webhook_secret() -> str:
    return _resolve("telegram_webhook_secret", get_settings().TELEGRAM_WEBHOOK_SECRET)


def _url(method: str) -> str:
    return _TELEGRAM_API.format(token=_bot_token(), method=method)


async def _send(text: str) -> None:
    """Send a Markdown message to the owner chat. No-op if not configured."""
    token = _bot_token()
    chat_id = _chat_id()
    if not token or not chat_id:
        return
    async with httpx.AsyncClient() as client:
        await client.post(
            _url("sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10.0,
        )


async def send_dynamic_registration_notice(
    client_id: str,
    client_name: str,
    redirect_uris: list[str],
) -> None:
    """Informational notice for dynamic client registrations (no approval buttons — client already created)."""
    await _send(
        f"⚡ *Dynamic Client Registered*\n\n"
        f"Client: *{client_name}*\n"
        f"ID: `{client_id}`\n"
        f"Redirect URIs: {', '.join(redirect_uris)}"
    )


async def send_registration_alert(
    company_name: str,
    contact_name: str,
    contact_email: str,
) -> None:
    """Inform the owner that a new client has self-registered (informational only — no approval needed)."""
    await _send(
        f"📋 *New Registration*\n\n"
        f"Company: *{company_name}*\n"
        f"Contact: {contact_name} — `{contact_email}`"
    )


async def send_topup_request_notice(
    user_id: str,
    user_email: str,
    amount: float,
    note: str,
    request_id: str,
) -> None:
    await _send(
        f"💳 *Credit Top-up Request*\n\n"
        f"User: `{user_email}` (`{user_id}`)\n"
        f"Amount: *{amount:.0f} credits*\n"
        f"Note: {note or '—'}\n\n"
        f"Review: /admin/topup-requests/{request_id}"
    )


async def register_webhook(webhook_url: str) -> None:
    """Register the webhook URL with Telegram on startup."""
    if not _bot_token():
        return
    payload: dict = {"url": webhook_url}
    secret = _webhook_secret()
    if secret:
        payload["secret_token"] = secret
    async with httpx.AsyncClient() as client:
        resp = await client.post(_url("setWebhook"), json=payload, timeout=10.0)
    data = resp.json()
    if not data.get("ok"):
        import sys
        print(f"WARNING: Telegram webhook registration failed: {data}", file=sys.stderr)

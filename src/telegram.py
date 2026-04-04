"""
Telegram Bot API wrapper for registration notifications.
Uses plain httpx — no telegram library dependency.
"""
from __future__ import annotations

import httpx

from src.config import get_settings

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _url(method: str) -> str:
    return _TELEGRAM_API.format(token=get_settings().TELEGRAM_BOT_TOKEN, method=method)


async def send_dynamic_registration_notice(
    client_id: str,
    client_name: str,
    redirect_uris: list[str],
) -> None:
    """Informational notice for dynamic client registrations (no approval buttons — client already created)."""
    settings = get_settings()
    text = (
        f"⚡ *Dynamic Client Registered*\n\n"
        f"Client: *{client_name}*\n"
        f"ID: `{client_id}`\n"
        f"Redirect URIs: {', '.join(redirect_uris)}"
    )
    async with httpx.AsyncClient() as client:
        await client.post(
            _url("sendMessage"),
            json={
                "chat_id": settings.TELEGRAM_OWNER_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10.0,
        )


async def send_registration_alert(
    company_name: str,
    contact_name: str,
    contact_email: str,
) -> None:
    """Inform the owner that a new client has self-registered (informational only — no approval needed)."""
    settings = get_settings()
    text = (
        f"📋 *New Registration*\n\n"
        f"Company: *{company_name}*\n"
        f"Contact: {contact_name} — `{contact_email}`"
    )
    async with httpx.AsyncClient() as client:
        await client.post(
            _url("sendMessage"),
            json={
                "chat_id": settings.TELEGRAM_OWNER_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10.0,
        )


async def register_webhook(webhook_url: str) -> None:
    """Register the webhook URL with Telegram on startup."""
    settings = get_settings()
    payload: dict = {"url": webhook_url}
    if settings.TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = settings.TELEGRAM_WEBHOOK_SECRET
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _url("setWebhook"),
            json=payload,
            timeout=10.0,
        )
    data = resp.json()
    if not data.get("ok"):
        import sys
        print(f"WARNING: Telegram webhook registration failed: {data}", file=sys.stderr)

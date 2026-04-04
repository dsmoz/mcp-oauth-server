"""
Telegram Bot API wrapper for OAuth consent approval notifications.
Uses plain httpx — no telegram library dependency.
"""
from __future__ import annotations

import httpx

from src.config import get_settings

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _url(method: str) -> str:
    return _TELEGRAM_API.format(token=get_settings().TELEGRAM_BOT_TOKEN, method=method)


async def send_approval_request(session_id: str, client_name: str, scopes: list[str]) -> int:
    """
    Send Approve/Deny inline keyboard to the owner's Telegram chat.
    Returns the message_id so it can be edited later.
    """
    settings = get_settings()
    scope_str = " ".join(scopes) if scopes else "mcp"
    text = (
        f"🔐 *Access Request*\n\n"
        f"Client: *{client_name}*\n"
        f"Scopes: `{scope_str}`\n\n"
        f"Tap to authorize or deny."
    )
    payload = {
        "chat_id": settings.TELEGRAM_OWNER_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{session_id}"},
                {"text": "❌ Deny",    "callback_data": f"deny:{session_id}"},
            ]]
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(_url("sendMessage"), json=payload, timeout=10.0)
    data = resp.json()
    return data["result"]["message_id"]


async def edit_message_result(message_id: int, result_text: str) -> None:
    """Remove inline buttons and send a follow-up text after decision."""
    settings = get_settings()
    chat_id = settings.TELEGRAM_OWNER_CHAT_ID
    async with httpx.AsyncClient() as client:
        # Remove buttons from original message
        await client.post(
            _url("editMessageReplyMarkup"),
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
            timeout=10.0,
        )
        # Send follow-up text
        await client.post(
            _url("sendMessage"),
            json={
                "chat_id": chat_id,
                "text": result_text,
                "parse_mode": "Markdown",
            },
            timeout=10.0,
        )


async def answer_callback(callback_id: str, text: str) -> None:
    """Acknowledge a callback_query (dismisses the loading spinner on the button)."""
    async with httpx.AsyncClient() as client:
        await client.post(
            _url("answerCallbackQuery"),
            json={"callback_query_id": callback_id, "text": text},
            timeout=10.0,
        )


async def send_registration_alert(
    request_id: str,
    company_name: str,
    contact_name: str,
    contact_email: str,
) -> None:
    """Notify the owner that a new registration request has been submitted."""
    settings = get_settings()
    text = (
        f"📋 *New Registration Request*\n\n"
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
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "✅ Approve", "callback_data": f"reg_approve:{request_id}"},
                        {"text": "❌ Reject",  "callback_data": f"reg_reject:{request_id}"},
                    ]]
                },
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

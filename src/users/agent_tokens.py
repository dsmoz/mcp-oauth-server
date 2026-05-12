"""Bearer agent tokens — long-lived API keys for non-OAuth agent clients.

Format: ``dsmoz_<32B base64url>``. Prefix stored as-is for display (first 14 chars);
raw token only returned once at creation. Lookups by SHA-256(token).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

from src.crypto import hash_token
from src.db import get_db


_TOKEN_PREFIX = "dsmoz_"


def _generate_raw_token() -> str:
    return _TOKEN_PREFIX + secrets.token_urlsafe(32)


class AgentTokenProvider:
    def __init__(self):
        self.db = get_db()

    def create(self, user_id: str, label: str) -> tuple[str, dict]:
        """Mint a token. Returns (raw_token, row). Raw token only available here."""
        raw = _generate_raw_token()
        row = {
            "user_id": user_id,
            "label": label.strip()[:120] or "Agent",
            "token_hash": hash_token(raw),
            "prefix": raw[:14],
        }
        result = self.db.table("user_agent_tokens").insert(row).execute()
        created = result.data[0] if result.data else row
        return raw, created

    def list_for_user(self, user_id: str) -> list[dict]:
        result = (
            self.db.table("user_agent_tokens")
            .select("id, label, prefix, created_at, last_used_at, revoked_at, expires_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return result.data or []

    def revoke(self, user_id: str, token_id: str) -> None:
        self.db.table("user_agent_tokens").update(
            {"revoked_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", token_id).eq("user_id", user_id).execute()

    def lookup(self, raw_token: str) -> Optional[dict]:
        """Return the token row if active (not revoked, not expired). None otherwise."""
        if not raw_token or not raw_token.startswith(_TOKEN_PREFIX):
            return None
        result = (
            self.db.table("user_agent_tokens")
            .select("*")
            .eq("token_hash", hash_token(raw_token))
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        row = result.data[0]
        if row.get("revoked_at"):
            return None
        exp = row.get("expires_at")
        if exp and exp < datetime.now(timezone.utc).isoformat():
            return None
        return row

    def touch_last_used(self, token_id: str) -> None:
        self.db.table("user_agent_tokens").update(
            {"last_used_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", token_id).execute()

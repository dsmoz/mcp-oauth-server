"""Microsoft Graph token management.

Stores per-user MS Graph refresh tokens (encrypted) and mints fresh access
tokens on demand for downstream proxying to mcp-microsoft365.

Public API:
    GRAPH_SCOPES         — list of delegated Graph scopes requested at consent.
    SCOPE_STR            — space-separated scope string (includes offline_access).
    exchange_code(...)   — turn an authorization code into tokens and persist.
    get_user_graph_token(user_id) -> str | None
                         — return a non-expired access_token, refreshing if needed.
    has_connection(user_id) -> bool
    disconnect(user_id)  — delete stored tokens for a user.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from cryptography.fernet import Fernet, InvalidToken

from src.config import get_settings
from src.db import get_db

# Delegated scopes required by mcp-microsoft365. Keep in sync with the
# CORE_SCOPES list previously defined in mcp-microsoft365/src/auth/device_flow.py.
GRAPH_SCOPES: list[str] = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read",
    "Mail.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.Read",
    "Calendars.ReadWrite",
    "Tasks.Read",
    "Tasks.ReadWrite",
    "Contacts.Read",
    "Contacts.ReadWrite",
    "Files.Read",
    "Files.ReadWrite",
    "People.Read",
]
SCOPE_STR: str = " ".join(GRAPH_SCOPES)

# Refresh access tokens this many seconds before their stated expiry.
_REFRESH_LEEWAY = 60

# Per-user locks to serialize concurrent refreshes for the same user.
_user_locks: dict[str, asyncio.Lock] = {}


# ── Encryption ───────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    key = get_settings().GRAPH_TOKEN_ENCRYPTION_KEY.strip()
    if not key:
        raise RuntimeError(
            "GRAPH_TOKEN_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
        )
    return Fernet(key.encode())


def _encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored MS refresh token could not be decrypted") from exc


# ── MS endpoints ─────────────────────────────────────────────────────────────

def _provider_config() -> tuple[str, str, str]:
    """(client_id, client_secret, tenant). Re-uses the social provider config."""
    from src.portal.social import _provider_creds  # local import avoids cycles
    cid, secret, tenant = _provider_creds("microsoft")
    if not cid or not secret:
        raise RuntimeError("Microsoft OAuth client_id/secret not configured")
    return cid, secret, (tenant or "common")


def _token_url(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


# ── DB access ────────────────────────────────────────────────────────────────

def _get_row(user_id: str) -> Optional[dict]:
    row = (
        get_db().table("user_ms_graph_tokens")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return (row.data or [None])[0]


def _upsert(
    *,
    user_id: str,
    refresh_token: str,
    access_token: str,
    expires_at: datetime,
    scope: str,
    ms_tenant_id: Optional[str],
    ms_upn: Optional[str],
) -> None:
    payload = {
        "user_id": user_id,
        "refresh_token_encrypted": _encrypt(refresh_token),
        "access_token": access_token,
        "expires_at": expires_at.isoformat(),
        "scope": scope,
        "ms_tenant_id": ms_tenant_id,
        "ms_user_principal_name": ms_upn,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    get_db().table("user_ms_graph_tokens").upsert(payload, on_conflict="user_id").execute()


# ── Public API ───────────────────────────────────────────────────────────────

def has_connection(user_id: str) -> bool:
    return _get_row(user_id) is not None


def disconnect(user_id: str) -> None:
    get_db().table("user_ms_graph_tokens").delete().eq("user_id", user_id).execute()


async def exchange_code(
    *,
    user_id: str,
    code: str,
    redirect_uri: str,
) -> dict:
    """Exchange an authorization code for tokens and persist them.

    Returns a small dict {scope, expires_at, ms_user_principal_name}.
    """
    cid, secret, tenant = _provider_config()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _token_url(tenant),
            data={
                "code": code,
                "client_id": cid,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": SCOPE_STR,
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code >= 400:
        print(f"GRAPH: token exchange failed {resp.status_code}: {resp.text}", file=sys.stderr)
        raise RuntimeError(f"Microsoft token exchange failed: {resp.text}")
    tokens = resp.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = int(tokens.get("expires_in", 3600))
    scope = tokens.get("scope", SCOPE_STR)
    if not access_token or not refresh_token:
        raise RuntimeError("Microsoft did not return access_token + refresh_token")

    # Lightweight identity lookup — best-effort.
    ms_upn: Optional[str] = None
    ms_tenant_id: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            me = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if me.status_code < 400:
            info = me.json()
            ms_upn = info.get("userPrincipalName") or info.get("mail")
        org = await _fetch_organization(access_token)
        ms_tenant_id = org
    except Exception:
        pass

    expires_at = datetime.now(timezone.utc).fromtimestamp(time.time() + expires_in, tz=timezone.utc)
    _upsert(
        user_id=user_id,
        refresh_token=refresh_token,
        access_token=access_token,
        expires_at=expires_at,
        scope=scope,
        ms_tenant_id=ms_tenant_id,
        ms_upn=ms_upn,
    )
    return {
        "scope": scope,
        "expires_at": expires_at.isoformat(),
        "ms_user_principal_name": ms_upn,
    }


async def _fetch_organization(access_token: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://graph.microsoft.com/v1.0/organization?$select=id",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        return None
    data = r.json()
    items = data.get("value") or []
    if not items:
        return None
    return items[0].get("id")


async def _refresh(row: dict) -> dict:
    """Refresh access token using the stored refresh_token. Returns updated row."""
    cid, secret, tenant = _provider_config()
    refresh_token = _decrypt(row["refresh_token_encrypted"])

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _token_url(tenant),
            data={
                "client_id": cid,
                "client_secret": secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": SCOPE_STR,
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code >= 400:
        print(f"GRAPH: refresh failed for {row['user_id']} {resp.status_code}: {resp.text}", file=sys.stderr)
        raise RuntimeError("Microsoft refresh failed — user must reconnect")

    tokens = resp.json()
    access_token = tokens["access_token"]
    new_refresh = tokens.get("refresh_token") or refresh_token  # MS may rotate
    expires_in = int(tokens.get("expires_in", 3600))
    scope = tokens.get("scope", row.get("scope") or SCOPE_STR)
    expires_at = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc)

    _upsert(
        user_id=row["user_id"],
        refresh_token=new_refresh,
        access_token=access_token,
        expires_at=expires_at,
        scope=scope,
        ms_tenant_id=row.get("ms_tenant_id"),
        ms_upn=row.get("ms_user_principal_name"),
    )
    return {
        **row,
        "access_token": access_token,
        "expires_at": expires_at.isoformat(),
        "scope": scope,
    }


def _parse_expires_at(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # ISO string from Supabase
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def get_user_graph_token(user_id: str) -> Optional[str]:
    """Return a non-expired Graph access token for the user, refreshing if needed.

    Returns None when the user has no MS connection — caller must signal to
    the user that they need to connect Microsoft 365 first.
    """
    row = _get_row(user_id)
    if not row:
        return None

    expires_at = _parse_expires_at(row["expires_at"])
    now = datetime.now(timezone.utc)
    if (expires_at - now).total_seconds() > _REFRESH_LEEWAY:
        return row["access_token"]

    lock = _user_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        # Re-check after acquiring the lock — another coroutine may have refreshed.
        row = _get_row(user_id) or row
        expires_at = _parse_expires_at(row["expires_at"])
        if (expires_at - datetime.now(timezone.utc)).total_seconds() > _REFRESH_LEEWAY:
            return row["access_token"]
        refreshed = await _refresh(row)
        return refreshed["access_token"]

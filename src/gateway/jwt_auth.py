"""jwt_auth.py — Supabase JWT verification and user identity resolution.

Scholar signs short-lived JWTs (5-min) with its Supabase project JWT secret.
This module verifies the signature, claims (iss, aud, exp, sub), resolves
or provisions gateway users, and exposes a FastAPI dependency for authed routes.

HS256 (Supabase's default) is the primary path — the secret is shared via env
(``SCHOLAR_SUPABASE_JWT_SECRET``). RS256/JWKS support is included as a
fall-forward when Supabase rolls out asymmetric signing per project.

Public API:
    JWTConfig                — env-resolved verification configuration
    InvalidJWT               — exception raised on any failure
    verify_supabase_jwt      — pure function; takes token + config, returns claims
    load_config_from_env     — convenience constructor for routes/middleware
    resolve_or_create_user   — map Supabase UUID to gateway user_id, insert if missing
    current_jwt_user         — FastAPI dependency: Bearer JWT → gateway user_id
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Optional

import jwt as pyjwt
from fastapi import HTTPException, Request, status
from jwt import (
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidTokenError,
    PyJWKClient,
)

from src.db import get_db


class InvalidJWT(Exception):
    """Raised when a JWT fails any verification step."""


@dataclass
class JWTConfig:
    issuer_allowlist: list[str]
    audience: str
    hs256_secret: Optional[str] = None
    jwks_url: Optional[str] = None
    leeway_s: int = 5
    _jwks_client: Optional[PyJWKClient] = field(default=None, init=False, repr=False)

    def jwks_client(self) -> Optional[PyJWKClient]:
        if not self.jwks_url:
            return None
        if self._jwks_client is None:
            self._jwks_client = PyJWKClient(self.jwks_url)
        return self._jwks_client


def load_config_from_env() -> JWTConfig:
    """Build a JWTConfig from env. Used by FastAPI dependency injection.

    Required env:
      SCHOLAR_JWT_ISS_ALLOWLIST   comma-separated issuer URLs
      SCHOLAR_JWT_AUDIENCE        expected aud claim (e.g. "gateway")
    One of:
      SCHOLAR_SUPABASE_JWT_SECRET HS256 shared secret (preferred today)
      SCHOLAR_JWT_JWKS_URL        RS256 JWKS endpoint (forward-compatible)
    """
    iss = os.getenv("SCHOLAR_JWT_ISS_ALLOWLIST", "").strip()
    aud = os.getenv("SCHOLAR_JWT_AUDIENCE", "").strip()
    if not iss or not aud:
        raise RuntimeError("SCHOLAR_JWT_ISS_ALLOWLIST / SCHOLAR_JWT_AUDIENCE not set")
    return JWTConfig(
        issuer_allowlist=[s.strip() for s in iss.split(",") if s.strip()],
        audience=aud,
        hs256_secret=os.getenv("SCHOLAR_SUPABASE_JWT_SECRET") or None,
        jwks_url=os.getenv("SCHOLAR_JWT_JWKS_URL") or None,
        leeway_s=int(os.getenv("SCHOLAR_JWT_LEEWAY_S", "5")),
    )


def _decode_with_secret(token: str, secret: str, audience: str, leeway: int) -> dict:
    return pyjwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        audience=audience,
        leeway=leeway,
        options={"require": ["exp", "iat", "iss", "sub", "aud"]},
    )


def _decode_with_jwks(token: str, client: PyJWKClient, audience: str, leeway: int) -> dict:
    signing_key = client.get_signing_key_from_jwt(token).key
    return pyjwt.decode(
        token,
        signing_key,
        algorithms=["RS256", "ES256"],
        audience=audience,
        leeway=leeway,
        options={"require": ["exp", "iat", "iss", "sub", "aud"]},
    )


def verify_supabase_jwt(token: str, config: JWTConfig) -> dict:
    """Verify a scholar-issued JWT. Returns claims on success, raises InvalidJWT.

    Verification steps:
      1. PyJWT decode (signature, exp, iat, aud, required claims).
      2. Issuer is in the allowlist.
      3. ``sub`` is present and non-empty.
    """
    if not token or token.count(".") != 2:
        raise InvalidJWT("malformed token")

    try:
        if config.hs256_secret:
            claims = _decode_with_secret(
                token, config.hs256_secret, config.audience, config.leeway_s
            )
        else:
            jwks = config.jwks_client()
            if jwks is None:
                raise InvalidJWT("no verifier configured")
            claims = _decode_with_jwks(token, jwks, config.audience, config.leeway_s)
    except ExpiredSignatureError as exc:
        raise InvalidJWT("expired") from exc
    except InvalidAudienceError as exc:
        raise InvalidJWT("audience mismatch") from exc
    except InvalidTokenError as exc:
        raise InvalidJWT(str(exc) or "invalid token") from exc

    iss = claims.get("iss", "")
    if iss not in config.issuer_allowlist:
        raise InvalidJWT(f"issuer mismatch: {iss!r}")

    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        raise InvalidJWT("missing sub claim")

    return claims


def resolve_or_create_user(db, supabase_user_id: str, email: str = "") -> str:
    """Map a Supabase user UUID to a gateway ``users.user_id`` (text).

    Resolution order, single shared wallet per real person:

    1. Existing scholar link by ``supabase_user_id`` -> return it.
    2. Existing gateway row with the SAME email -> share that wallet.
       The Scholar JWT is only minted after Supabase verified the email,
       so the email here is trusted. ``is_active`` is the gateway's
       verified/confirmed flag (set by social sign-in and password
       confirmation):
         - active row  -> a native Connect account for the same person;
           link ``supabase_user_id`` onto it (true shared wallet).
         - inactive row with zero balance -> an abandoned/unconfirmed
           shell; reclaim it (link + activate). This also avoids the
           ``UNIQUE(email)`` collision a fresh insert would hit.
         - inactive row that still holds credit -> anomalous (possible
           deactivation); refuse rather than absorb the balance.
       A row already linked to a *different* supabase_user_id is the one
       case we refuse to merge (one verified email = one person; this
       should be unreachable) and raise rather than cross wallets.
    3. No match -> insert a new ``scholar_`` + 12-hex-char row.

    Idempotent under race via the supabase_user_id unique index.
    """
    result = (
        db.table("users")
        .select("user_id")
        .eq("supabase_user_id", supabase_user_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["user_id"]

    norm_email = (email or "").strip().lower()
    if norm_email:
        existing = (
            db.table("users")
            .select("user_id, is_active, supabase_user_id, credit_balance")
            .eq("email", norm_email)
            .limit(1)
            .execute()
        )
        row = existing.data[0] if existing.data else None
        if row is not None:
            linked = row.get("supabase_user_id")
            if linked and linked != supabase_user_id:
                raise RuntimeError(
                    "email already linked to a different supabase user; "
                    "refusing to merge wallets"
                )
            if row.get("is_active"):
                # Native Connect account for the same verified person ->
                # link supabase_user_id onto it (true shared wallet).
                (
                    db.table("users")
                    .update({"supabase_user_id": supabase_user_id})
                    .eq("user_id", row["user_id"])
                    .execute()
                )
                return row["user_id"]
            # Inactive shell: reclaim ONLY a genuinely empty one (link +
            # activate), avoiding the UNIQUE(email) insert collision. An
            # inactive row that still holds credit is anomalous (e.g. a
            # deactivated/suspended account) -> refuse rather than absorb its
            # balance under a fresh supabase identity.
            if (row.get("credit_balance") or 0) > 0:
                raise RuntimeError(
                    "inactive account holds a credit balance; "
                    "refusing to reclaim wallet"
                )
            (
                db.table("users")
                .update({"supabase_user_id": supabase_user_id, "is_active": True})
                .eq("user_id", row["user_id"])
                .execute()
            )
            return row["user_id"]

    new_user_id = "scholar_" + secrets.token_hex(6)
    try:
        ins = (
            db.table("users")
            .insert({
                "user_id": new_user_id,
                "email": norm_email or f"{new_user_id}@scholar.dsmoz.local",
                "supabase_user_id": supabase_user_id,
                "is_active": True,
            })
            .execute()
        )
    except Exception:
        retry = (
            db.table("users")
            .select("user_id")
            .eq("supabase_user_id", supabase_user_id)
            .limit(1)
            .execute()
        )
        if retry.data:
            return retry.data[0]["user_id"]
        raise
    return ins.data[0]["user_id"]


_CONFIG: Optional[JWTConfig] = None


def _get_config() -> JWTConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config_from_env()
    return _CONFIG


def current_jwt_user(request: Request) -> str:
    """FastAPI dependency. Returns gateway ``users.user_id`` for a JWT-authed request.

    Reads ``Authorization: Bearer <jwt>``. Verifies signature/claims, resolves
    or provisions the gateway users row keyed by Supabase UUID. 401 on any
    failure.
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_bearer")
    token = auth[7:].strip()
    try:
        claims = verify_supabase_jwt(token, _get_config())
    except InvalidJWT as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid_jwt: {exc}")
    db = get_db()
    return resolve_or_create_user(db, claims["sub"], email=claims.get("email", ""))

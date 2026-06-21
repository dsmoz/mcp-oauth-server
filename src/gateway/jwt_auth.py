"""jwt_auth.py — Supabase JWT verification for scholar-first identity bridge.

Scholar signs short-lived JWTs (5-min) with its Supabase project JWT secret.
This module verifies the signature, claims (iss, aud, exp, sub), and exposes
``verify_supabase_jwt(token, config) -> claims dict``.

HS256 (Supabase's default) is the primary path — the secret is shared via env
(``SCHOLAR_SUPABASE_JWT_SECRET``). RS256/JWKS support is included as a
fall-forward when Supabase rolls out asymmetric signing per project.

Public API:
    JWTConfig            — env-resolved verification configuration
    InvalidJWT           — exception raised on any failure
    verify_supabase_jwt  — pure function; takes token + config, returns claims
    load_config_from_env — convenience constructor for routes/middleware
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import jwt as pyjwt
from jwt import (
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidTokenError,
    PyJWKClient,
)


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
    except InvalidIssuerError as exc:
        raise InvalidJWT("issuer mismatch") from exc
    except InvalidTokenError as exc:
        raise InvalidJWT(str(exc) or "invalid token") from exc

    iss = claims.get("iss", "")
    if iss not in config.issuer_allowlist:
        raise InvalidJWT(f"issuer mismatch: {iss!r}")

    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        raise InvalidJWT("missing sub claim")

    return claims

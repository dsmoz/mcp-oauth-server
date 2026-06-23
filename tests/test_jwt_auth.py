"""Supabase JWT verification — gateway accepts scholar-signed JWTs."""
import time
import uuid
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest

from src.gateway.jwt_auth import (
    InvalidJWT,
    JWTConfig,
    resolve_or_create_user,
    verify_supabase_jwt,
)


HS256_SECRET = "test-secret-do-not-use-in-prod"
ISS = "https://scholar-test.supabase.co/auth/v1"
AUD = "gateway"


def _mint(payload: dict, secret: str = HS256_SECRET, alg: str = "HS256") -> str:
    return pyjwt.encode(payload, secret, algorithm=alg)


def _config() -> JWTConfig:
    return JWTConfig(
        issuer_allowlist=[ISS],
        audience=AUD,
        hs256_secret=HS256_SECRET,
        leeway_s=5,
    )


def test_valid_jwt_returns_sub():
    now = int(time.time())
    token = _mint({
        "sub": "11111111-1111-1111-1111-111111111111",
        "iss": ISS,
        "aud": AUD,
        "iat": now,
        "exp": now + 60,
    })
    claims = verify_supabase_jwt(token, _config())
    assert claims["sub"] == "11111111-1111-1111-1111-111111111111"


def test_expired_jwt_rejected():
    now = int(time.time())
    token = _mint({
        "sub": "11111111-1111-1111-1111-111111111111",
        "iss": ISS, "aud": AUD, "iat": now - 120, "exp": now - 60,
    })
    with pytest.raises(InvalidJWT, match="expired"):
        verify_supabase_jwt(token, _config())


def test_wrong_audience_rejected():
    now = int(time.time())
    token = _mint({
        "sub": "u", "iss": ISS, "aud": "other", "iat": now, "exp": now + 60,
    })
    with pytest.raises(InvalidJWT, match="audience"):
        verify_supabase_jwt(token, _config())


def test_wrong_issuer_rejected():
    now = int(time.time())
    token = _mint({
        "sub": "u", "iss": "https://evil.example/auth", "aud": AUD,
        "iat": now, "exp": now + 60,
    })
    with pytest.raises(InvalidJWT, match="issuer"):
        verify_supabase_jwt(token, _config())


def test_missing_sub_rejected():
    now = int(time.time())
    token = _mint({"iss": ISS, "aud": AUD, "iat": now, "exp": now + 60})
    with pytest.raises(InvalidJWT, match="sub"):
        verify_supabase_jwt(token, _config())


def test_garbage_token_rejected():
    with pytest.raises(InvalidJWT):
        verify_supabase_jwt("not.a.jwt", _config())


def test_wrong_secret_rejected():
    now = int(time.time())
    token = _mint({
        "sub": "u", "iss": ISS, "aud": AUD, "iat": now, "exp": now + 60,
    }, secret="wrong-secret-key")
    with pytest.raises(InvalidJWT):
        verify_supabase_jwt(token, _config())


def test_resolve_existing_user():
    sup = uuid.uuid4()
    db = MagicMock()
    db.table().select().eq().limit().execute.return_value.data = [
        {"user_id": "u_existing", "supabase_user_id": str(sup)}
    ]
    user_id = resolve_or_create_user(db, str(sup), email="x@y")
    assert user_id == "u_existing"


def _res(data):
    """A stand-in for a supabase-py execute() result holding ``.data``."""
    r = MagicMock()
    r.data = data
    return r


def test_resolve_creates_new_user():
    sup = uuid.uuid4()
    db = MagicMock()
    # Both select queries (by supabase_user_id, then by email) miss.
    db.table().select().eq().limit().execute.return_value.data = []
    db.table().insert().execute.return_value.data = [
        {"user_id": "u_new", "supabase_user_id": str(sup)}
    ]
    user_id = resolve_or_create_user(db, str(sup), email="x@y")
    assert user_id == "u_new"


def test_resolve_merges_verified_email():
    """A verified Connect account with the same email shares its wallet."""
    sup = uuid.uuid4()
    db = MagicMock()
    db.table().select().eq().limit().execute.side_effect = [
        _res([]),  # supabase_user_id lookup -> miss
        _res([{"user_id": "u_connect", "is_active": True,
               "supabase_user_id": None, "credit_balance": 12.5}]),
    ]
    user_id = resolve_or_create_user(db, str(sup), email="Dan@Example.com")
    assert user_id == "u_connect"
    # Linked onto the existing row; an active wallet keeps its balance and is
    # not re-activated or re-inserted.
    db.table().update.assert_called_with({"supabase_user_id": str(sup)})
    db.table().insert.assert_not_called()


def test_resolve_reclaims_unverified_shell():
    """An inactive, zero-balance shell holding the email is reclaimed."""
    sup = uuid.uuid4()
    db = MagicMock()
    db.table().select().eq().limit().execute.side_effect = [
        _res([]),  # supabase_user_id lookup -> miss
        _res([{"user_id": "u_shell", "is_active": False,
               "supabase_user_id": None, "credit_balance": 0}]),
    ]
    user_id = resolve_or_create_user(db, str(sup), email="dan@example.com")
    assert user_id == "u_shell"
    db.table().update.assert_called_with(
        {"supabase_user_id": str(sup), "is_active": True}
    )
    db.table().insert.assert_not_called()


def test_resolve_refuses_reclaim_of_inactive_with_balance():
    """An inactive row holding credit is never absorbed — it raises."""
    sup = uuid.uuid4()
    db = MagicMock()
    db.table().select().eq().limit().execute.side_effect = [
        _res([]),  # supabase_user_id lookup -> miss
        _res([{"user_id": "u_frozen", "is_active": False,
               "supabase_user_id": None, "credit_balance": 7.0}]),
    ]
    with pytest.raises(RuntimeError, match="credit balance"):
        resolve_or_create_user(db, str(sup), email="dan@example.com")
    db.table().update.assert_not_called()
    db.table().insert.assert_not_called()


def test_resolve_refuses_cross_wallet_merge():
    """Email already linked to a different supabase user is never merged."""
    sup = uuid.uuid4()
    other = uuid.uuid4()
    db = MagicMock()
    db.table().select().eq().limit().execute.side_effect = [
        _res([]),  # supabase_user_id lookup -> miss
        _res([{"user_id": "u_other", "is_active": True,
               "supabase_user_id": str(other), "credit_balance": 0}]),
    ]
    with pytest.raises(RuntimeError, match="different supabase user"):
        resolve_or_create_user(db, str(sup), email="dan@example.com")

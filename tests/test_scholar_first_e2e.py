"""End-to-end: scholar JWT → wallet balance.

Requires SCHOLAR_SUPABASE_JWT_SECRET + SCHOLAR_JWT_AUDIENCE +
SCHOLAR_JWT_ISS_ALLOWLIST in test env, plus a live Supabase connection
configured via the standard env vars used by src.db.get_db().

Gated on RUN_GATEWAY_E2E=1 to keep CI fast and offline by default.
"""
import os
import time

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(scope="module")
def jwt_env():
    secret = "e2e-test-secret"
    iss = "https://scholar-test.supabase.co/auth/v1"
    aud = "gateway"
    os.environ["SCHOLAR_SUPABASE_JWT_SECRET"] = secret
    os.environ["SCHOLAR_JWT_AUDIENCE"] = aud
    os.environ["SCHOLAR_JWT_ISS_ALLOWLIST"] = iss
    from src.gateway import jwt_auth
    jwt_auth._CONFIG = None
    yield secret, iss, aud


def _make_jwt(secret: str, iss: str, aud: str, sub: str) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"sub": sub, "iss": iss, "aud": aud, "iat": now, "exp": now + 60},
        secret,
        algorithm="HS256",
    )


@pytest.mark.skipif(
    not os.getenv("RUN_GATEWAY_E2E"),
    reason="Set RUN_GATEWAY_E2E=1 to run against test Supabase",
)
def test_balance_with_real_jwt(jwt_env):
    secret, iss, aud = jwt_env
    sub = "00000000-0000-0000-0000-000000000099"
    token = _make_jwt(secret, iss, aud, sub)
    with TestClient(app) as client:
        r = client.get(
            "/api/v1/wallet/balance",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200
    body = r.json()
    assert "balance_credits" in body
    assert "balance_usd" in body

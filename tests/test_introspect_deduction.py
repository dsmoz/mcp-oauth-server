"""Unit tests for the /introspect endpoint's per-call credit deduction.

These tests exercise the four documented branches:

* No ``cost`` field → behaviour identical to today (no deduction, no extra fields).
* ``cost > 0`` with sufficient balance → 200, ``active=true``, ``credits_remaining`` set.
* ``cost > 0`` with insufficient balance → 200, ``active=false``, ``reason=insufficient_credits``.
* ``cost > 0`` and the RPC raises → 200, ``active=false``, ``reason=billing_error``.

The Supabase provider, the DB client, and the settings module are stubbed so
the tests run without any network or env-var configuration.
"""
from __future__ import annotations

import os
from typing import Any, Optional

# Settings requires these — set before importing config.
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
os.environ.setdefault("INTROSPECT_SECRET", "test-introspect-secret")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_settings
from src.models import AccessToken
from src.oauth import routes as oauth_routes


# ── Test doubles ─────────────────────────────────────────────────────────────

class FakeProvider:
    """Returns a pre-seeded AccessToken for any token string."""

    def __init__(self, token: Optional[AccessToken]) -> None:
        self._token = token

    def load_access_token(self, _: str) -> Optional[AccessToken]:
        return self._token


class FakeRPC:
    """Captures RPC calls and replays a configured response.

    ``mode`` is one of:
      * ``"ok"`` — returns ``balance`` from .execute()
      * ``"insufficient"`` — returns -1 from .execute()
      * ``"raise"`` — raises RuntimeError from .execute()
    """

    def __init__(self, mode: str = "ok", balance: float = 99.5) -> None:
        self.mode = mode
        self.balance = balance
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name: str, params: dict) -> "FakeRPC":
        self.calls.append((name, params))
        return self

    def execute(self) -> Any:
        if self.mode == "raise":
            raise RuntimeError("rpc down")

        class _Result:
            pass

        r = _Result()
        r.data = -1 if self.mode == "insufficient" else self.balance
        return r


class FakeTable:
    """Swallows inserts on oauth_usage_logs."""

    def __init__(self, log: list) -> None:
        self._log = log

    def insert(self, row: dict) -> "FakeTable":
        self._log.append(row)
        return self

    def execute(self) -> None:
        return None


class FakeDB:
    def __init__(self, rpc: FakeRPC) -> None:
        self._rpc = rpc
        self.inserted: list[dict] = []

    def rpc(self, name: str, params: dict):
        return self._rpc(name, params)

    def table(self, _name: str) -> FakeTable:
        return FakeTable(self.inserted)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def access_token() -> AccessToken:
    return AccessToken(
        token="tok-abc",
        client_id="client-1",
        user_id="user-1",
        scopes=["mcp", "linguist"],
        expires_at=10_000_000_000,  # far future
        is_revoked=False,
    )


def _make_app(monkeypatch, token: Optional[AccessToken], rpc: FakeRPC) -> tuple[TestClient, FakeDB]:
    """Build a minimal FastAPI app mounting only the oauth router with stubbed deps."""
    fake_db = FakeDB(rpc)
    monkeypatch.setattr(oauth_routes, "_provider", lambda: FakeProvider(token))
    monkeypatch.setattr(oauth_routes, "get_db", lambda: fake_db)
    # Ensure the settings cache returns a known introspect secret.
    get_settings.cache_clear()
    monkeypatch.setenv("INTROSPECT_SECRET", "test-introspect-secret")
    app = FastAPI()
    app.include_router(oauth_routes.router)
    return TestClient(app), fake_db


HEADERS = {"x-introspect-secret": "test-introspect-secret"}


# ── Tests ───────────────────────────────────────────────────────────────────

def test_no_cost_is_unchanged(monkeypatch, access_token):
    """Omitting ``cost`` must reproduce the pre-feature behaviour."""
    rpc = FakeRPC(mode="raise")  # would blow up if called
    client, db = _make_app(monkeypatch, access_token, rpc)

    resp = client.post("/introspect", json={"token": "tok-abc"}, headers=HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["user_id"] == "user-1"
    assert "credits_remaining" not in body
    assert rpc.calls == []  # no deduction attempted
    # Usage log row should have credits_used=0 and no endpoint override.
    assert db.inserted and db.inserted[0]["credits_used"] == 0


def test_sufficient_balance_deducts_and_returns_remaining(monkeypatch, access_token):
    rpc = FakeRPC(mode="ok", balance=42.0)
    client, db = _make_app(monkeypatch, access_token, rpc)

    resp = client.post(
        "/introspect",
        json={"token": "tok-abc", "cost": 0.5, "upstream": "linguist", "units": 1200},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["credits_remaining"] == 42.0
    assert rpc.calls == [("deduct_credits_user", {"p_user_id": "user-1", "p_amount": 0.5})]
    # Usage log captures cost + upstream endpoint label.
    logged = db.inserted[0]
    assert logged["credits_used"] == 0.5
    assert logged["endpoint"] == "introspect/linguist"


def test_insufficient_balance_returns_reason(monkeypatch, access_token):
    rpc = FakeRPC(mode="insufficient")
    client, _ = _make_app(monkeypatch, access_token, rpc)

    resp = client.post(
        "/introspect",
        json={"token": "tok-abc", "cost": 999_999.0, "upstream": "linguist"},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"active": False, "reason": "insufficient_credits"}


def test_rpc_exception_returns_billing_error(monkeypatch, access_token):
    rpc = FakeRPC(mode="raise")
    client, _ = _make_app(monkeypatch, access_token, rpc)

    resp = client.post(
        "/introspect",
        json={"token": "tok-abc", "cost": 1.0, "upstream": "linguist"},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"active": False, "reason": "billing_error"}


def test_zero_cost_is_treated_as_no_deduction(monkeypatch, access_token):
    """``cost=0`` should not call the RPC and should not surface credits_remaining."""
    rpc = FakeRPC(mode="raise")
    client, _ = _make_app(monkeypatch, access_token, rpc)

    resp = client.post(
        "/introspect",
        json={"token": "tok-abc", "cost": 0},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert "credits_remaining" not in body
    assert rpc.calls == []

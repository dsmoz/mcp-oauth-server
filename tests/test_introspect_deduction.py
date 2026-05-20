"""Unit tests for the /introspect endpoint.

As of pricing model v1 (2026-05-20), the gateway is the sole biller — the
introspect endpoint no longer deducts credits, even when ``cost`` is supplied.
These tests verify:

* Valid token + no cost → active=true, no credits_remaining, no RPC call.
* Valid token + cost > 0 → SAME response (cost is ignored, no RPC call).
* Inactive/expired token → active=false.
* Usage log records credits_used=0 regardless of supplied cost.

The Supabase provider, the DB client, and the settings module are stubbed so
the tests run without any network or env-var configuration.
"""
from __future__ import annotations

import os
from typing import Any, Optional

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
os.environ.setdefault("INTROSPECT_SECRET", "test-introspect-secret")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_settings
from src.models import AccessToken
from src.oauth import routes as oauth_routes


class FakeProvider:
    def __init__(self, token: Optional[AccessToken]) -> None:
        self._token = token

    def load_access_token(self, _: str) -> Optional[AccessToken]:
        return self._token


class FakeQuery:
    """Stub for table().select().eq().limit().execute() chains."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def select(self, *_a, **_kw): return self
    def eq(self, *_a, **_kw): return self
    def limit(self, *_a, **_kw): return self

    def execute(self):
        class _R:
            pass
        r = _R()
        r.data = self._rows
        return r


class FakeTable:
    def __init__(self, log: list, rows: list[dict] | None = None) -> None:
        self._log = log
        self._rows = rows or []

    def insert(self, row: dict) -> "FakeTable":
        self._log.append(row)
        return self

    def execute(self) -> None:
        return None

    def select(self, *_a, **_kw):
        return FakeQuery(self._rows)


class FakeDB:
    def __init__(self) -> None:
        self.inserted: list[dict] = []
        self.rpc_calls: list[tuple[str, dict]] = []

    def rpc(self, name: str, params: dict):
        self.rpc_calls.append((name, params))
        raise AssertionError("introspect must not call any RPC (gateway is sole biller)")

    def table(self, name: str) -> FakeTable:
        if name == "users":
            return FakeTable(self.inserted, rows=[{"is_admin": False}])
        return FakeTable(self.inserted)


@pytest.fixture
def access_token() -> AccessToken:
    return AccessToken(
        token="tok-abc",
        client_id="client-1",
        user_id="user-1",
        scopes=["mcp", "linguist"],
        expires_at=10_000_000_000,
        is_revoked=False,
    )


def _make_app(monkeypatch, token: Optional[AccessToken]) -> tuple[TestClient, FakeDB]:
    fake_db = FakeDB()
    monkeypatch.setattr(oauth_routes, "_provider", lambda: FakeProvider(token))
    monkeypatch.setattr(oauth_routes, "get_db", lambda: fake_db)
    get_settings.cache_clear()
    monkeypatch.setenv("INTROSPECT_SECRET", "test-introspect-secret")
    app = FastAPI()
    app.include_router(oauth_routes.router)
    return TestClient(app), fake_db


HEADERS = {"x-introspect-secret": "test-introspect-secret"}


def test_no_cost_returns_active(monkeypatch, access_token):
    client, db = _make_app(monkeypatch, access_token)

    resp = client.post("/introspect", json={"token": "tok-abc"}, headers=HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["user_id"] == "user-1"
    assert "credits_remaining" not in body
    assert db.rpc_calls == []
    assert db.inserted and db.inserted[0]["credits_used"] == 0


def test_cost_field_is_ignored(monkeypatch, access_token):
    """Gateway is sole biller — cost must be ignored, no deduction attempted."""
    client, db = _make_app(monkeypatch, access_token)

    resp = client.post(
        "/introspect",
        json={"token": "tok-abc", "cost": 999.0, "upstream": "linguist"},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert "credits_remaining" not in body
    assert db.rpc_calls == []
    logged = db.inserted[0]
    assert logged["credits_used"] == 0
    assert logged["endpoint"] == "introspect/linguist"


def test_zero_cost_is_ok(monkeypatch, access_token):
    client, db = _make_app(monkeypatch, access_token)

    resp = client.post(
        "/introspect", json={"token": "tok-abc", "cost": 0}, headers=HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert "credits_remaining" not in body
    assert db.rpc_calls == []


def test_missing_token_returns_inactive(monkeypatch):
    client, _ = _make_app(monkeypatch, None)

    resp = client.post("/introspect", json={"token": "tok-abc"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"active": False}

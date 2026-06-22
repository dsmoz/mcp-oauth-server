"""Admin internal service API — grant credits idempotency + usage read.

The credits endpoint is idempotent on request_id (backed by the UNIQUE index on
oauth_usage_logs.request_id). These tests cover the early-return path without a
live DB by monkeypatching the module-level helpers.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.gateway.admin_internal_api as mod
from src.portal.routes import _require_service_secret


def _make_client(monkeypatch, *, already_logged: bool, balance: float = 42.0):
    """Mount the admin router with auth bypassed and helpers stubbed."""
    provider = MagicMock()
    provider.get_user.return_value = MagicMock(credit_balance=balance)

    monkeypatch.setattr(mod, "get_db", lambda: MagicMock())
    monkeypatch.setattr(mod, "resolve_or_create_user", lambda db, sid, email="": "gw-1")
    monkeypatch.setattr(mod, "_already_logged", lambda db, rid: already_logged)
    monkeypatch.setattr(mod, "SupabaseUserProvider", lambda: provider)

    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[_require_service_secret] = lambda: None
    return TestClient(app), provider


def _body(**over):
    b = {
        "supabase_user_id": "00000000-0000-0000-0000-000000000001",
        "amount": 10.0,
        "request_id": "req-abc",
    }
    b.update(over)
    return b


def test_grant_credits_idempotent_early_return(monkeypatch):
    """Duplicate request_id: no second grant, returns current balance + idempotent."""
    client, provider = _make_client(monkeypatch, already_logged=True, balance=42.0)

    r = client.post("/api/internal/admin/credits", json=_body())

    assert r.status_code == 200
    body = r.json()
    assert body == {
        "user_id": "gw-1",
        "new_balance_credits": 42.0,
        "credited": 0.0,
        "idempotent": True,
    }
    provider.add_credits.assert_not_called()


def test_grant_credits_fresh_grant(monkeypatch):
    """First call for a request_id: grants once and reports idempotent=False."""
    client, provider = _make_client(monkeypatch, already_logged=False, balance=52.0)

    r = client.post("/api/internal/admin/credits", json=_body(amount=10.0))

    assert r.status_code == 200
    body = r.json()
    assert body["idempotent"] is False
    assert body["credited"] == 10.0
    assert body["new_balance_credits"] == 52.0
    provider.add_credits.assert_called_once_with("gw-1", 10.0)


def test_grant_credits_requires_request_id(monkeypatch):
    """request_id is mandatory — omitting it is a 422 validation error."""
    client, _ = _make_client(monkeypatch, already_logged=False)
    body = _body()
    del body["request_id"]

    r = client.post("/api/internal/admin/credits", json=body)

    assert r.status_code == 422

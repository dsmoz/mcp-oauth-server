"""Wallet API endpoints — balance and topup packages."""
from __future__ import annotations

import os
from typing import Optional
from unittest.mock import MagicMock

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
os.environ.setdefault("SCHOLAR_JWT_ISS_ALLOWLIST", "https://scholar.supabase.co/auth/v1")
os.environ.setdefault("SCHOLAR_JWT_AUDIENCE", "gateway")
os.environ.setdefault("SCHOLAR_SUPABASE_JWT_SECRET", "test-jwt-secret")

import pytest
import time
import jwt as pyjwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.jwt_auth import current_jwt_user
from src.config import get_settings


ISS = "https://scholar.supabase.co/auth/v1"
AUD = "gateway"
JWT_SECRET = "test-jwt-secret"


def _mint_jwt(sub: str, email: str = "") -> str:
    """Create a valid test JWT."""
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": sub,
            "iss": ISS,
            "aud": AUD,
            "iat": now,
            "exp": now + 3600,
            "email": email or f"{sub}@scholar.local",
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def _make_app_with_mocks(monkeypatch):
    """Create FastAPI instance with wallet router + mocked dependencies."""
    # Import router after env is set
    from src.gateway.wallet_api import router as wallet_router
    from src.db import get_db

    # Mock get_db
    mock_db = MagicMock()

    # Create app and include wallet router
    app = FastAPI()
    app.include_router(wallet_router)

    # Override current_jwt_user dependency — must accept Request param
    def mock_current_jwt_user(request=None) -> str:
        return "user-test-123"

    app.dependency_overrides[current_jwt_user] = mock_current_jwt_user

    # Override get_db dependency
    app.dependency_overrides[get_db] = lambda: mock_db

    return TestClient(app), mock_db


def test_wallet_balance_success(monkeypatch):
    """GET /api/v1/wallet/balance returns credit balance + USD equiv."""
    client, mock_db = _make_app_with_mocks(monkeypatch)

    # Mock users and pricing_config tables
    def mock_table(table_name):
        mock_table_obj = MagicMock()
        if table_name == "pricing_config":
            # Get usd_per_credit from config (id=1)
            mock_table_obj.select().eq().limit().execute.return_value.data = [
                {"usd_per_credit": 0.01}
            ]
        elif table_name == "users":
            # Get credit_balance for user
            mock_table_obj.select().eq().limit().execute.return_value.data = [
                {"credit_balance": 1000.0}
            ]
        return mock_table_obj

    mock_db.table = mock_table

    response = client.get(
        "/api/v1/wallet/balance",
        headers={"Authorization": "Bearer " + _mint_jwt("user-123")}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["balance_credits"] == 1000.0
    assert data["balance_usd"] == 10.0  # 1000 * 0.01
    assert data["currency"] == "USD"


def test_wallet_balance_missing_jwt(monkeypatch):
    """GET /api/v1/wallet/balance without JWT returns 401."""
    # Create app without mocking JWT to test real auth
    from src.gateway.wallet_api import router as wallet_router

    app = FastAPI()
    app.include_router(wallet_router)
    client = TestClient(app)

    response = client.get("/api/v1/wallet/balance")
    assert response.status_code == 401


def test_wallet_packages_success(monkeypatch):
    """GET /api/v1/wallet/packages returns published topup packages sorted by price."""
    client, mock_db = _make_app_with_mocks(monkeypatch)

    # Mock topup_packages table select with real DB columns
    # (price_amount, credits, currency, is_published)
    packages = [
        {"id": "pkg-1", "name": "Small", "price_amount": 1.0, "credits": 100, "currency": "USD", "is_published": True},
        {"id": "pkg-3", "name": "Large", "price_amount": 8.0, "credits": 1000, "currency": "USD", "is_published": True},
        {"id": "pkg-2", "name": "Medium", "price_amount": 4.0, "credits": 500, "currency": "USD", "is_published": True},
    ]

    def mock_table(table_name):
        mock_table_obj = MagicMock()
        if table_name == "topup_packages":
            # Simulate order by price_amount (ascending)
            mock_table_obj.select().eq().order().execute.return_value.data = [
                packages[0],
                packages[2],
                packages[1],
            ]
        return mock_table_obj

    mock_db.table = mock_table

    response = client.get(
        "/api/v1/wallet/packages",
        headers={"Authorization": "Bearer " + _mint_jwt("user-123")}
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["packages"]) == 3
    # Verify sorted by price and response field names
    prices = [p["price_usd"] for p in data["packages"]]
    assert prices == [1.0, 4.0, 8.0]
    # Verify credits_granted field
    credits = [p["credits_granted"] for p in data["packages"]]
    assert credits == [100.0, 500.0, 1000.0]


def test_wallet_packages_missing_jwt(monkeypatch):
    """GET /api/v1/wallet/packages without JWT returns 401."""
    # Create app without mocking JWT to test real auth
    from src.gateway.wallet_api import router as wallet_router

    app = FastAPI()
    app.include_router(wallet_router)
    client = TestClient(app)

    response = client.get("/api/v1/wallet/packages")
    assert response.status_code == 401

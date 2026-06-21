from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def jwt_user():
    from src.gateway.jwt_auth import current_jwt_user
    # app is wrapped in GatewayASGI; access the underlying FastAPI app
    fastapi_app = app.app if hasattr(app, 'app') else app
    fastapi_app.dependency_overrides[current_jwt_user] = lambda: "u_test"
    yield "u_test"
    fastapi_app.dependency_overrides.clear()


def test_mint_creates_row_and_returns_token(client, jwt_user):
    with patch("src.gateway.wallet_api.get_db") as mock_db:
        mock_db.return_value.table.return_value.insert.return_value.execute.return_value.data = [
            {"token_id": "00000000-0000-0000-0000-000000000001"}
        ]
        r = client.post(
            "/api/v1/tokens/mint",
            json={"scope": "mcp-scholar", "label": "Claude Desktop"},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("scl_")
    assert len(body["token"]) >= 40
    assert body["scope"] == "mcp-scholar"
    assert body["token_id"]


def test_list_tokens(client, jwt_user):
    with patch("src.gateway.wallet_api.get_db") as mock_db:
        mock_db.return_value.table.return_value.select.return_value.eq.return_value.is_.return_value.order.return_value.execute.return_value.data = [
            {
                "token_id": "t1",
                "label": "Claude",
                "scope": "mcp-scholar",
                "expires_at": None,
                "created_at": "2026-06-21T00:00:00Z",
            }
        ]
        r = client.get("/api/v1/tokens", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["tokens"][0]["label"] == "Claude"


def test_revoke_token(client, jwt_user):
    with patch("src.gateway.wallet_api.get_db") as mock_db:
        mock_db.return_value.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{"token_id": "t1"}]
        r = client.delete("/api/v1/tokens/t1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["revoked"] is True

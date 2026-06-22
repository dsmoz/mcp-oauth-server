from unittest.mock import patch

import pytest

from src.gateway.stripe_client import (
    StripeNotConfigured,
    create_checkout_session,
    verify_webhook_event,
)


def test_create_checkout_session_uses_env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    with patch("src.gateway.stripe_client.stripe.checkout.Session.create") as mock_create:
        mock_create.return_value = type("S", (), {"id": "cs_test_1", "url": "https://stripe/x"})()
        out = create_checkout_session(
            user_id="u1",
            package_id="p1",
            unit_amount_cents=500,
            credits=500,
            success_url="https://scholar/ok",
            cancel_url="https://scholar/cancel",
        )
    assert out["session_id"] == "cs_test_1"
    assert out["checkout_url"] == "https://stripe/x"


def test_missing_env_raises(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    with pytest.raises(StripeNotConfigured):
        create_checkout_session(
            user_id="u1", package_id="p1", unit_amount_cents=500,
            credits=500, success_url="x", cancel_url="y",
        )

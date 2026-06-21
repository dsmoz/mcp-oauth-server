from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def _fake_event(event_type: str, data: dict) -> dict:
    return {"id": "evt_1", "type": event_type, "data": {"object": data}}


def test_checkout_completed_credits_wallet(client):
    event = _fake_event("checkout.session.completed", {
        "id": "cs_1",
        "payment_intent": "pi_1",
        "client_reference_id": "u_test",
        "metadata": {"user_id": "u_test", "package_id": "p1", "credits": "500"},
        "amount_total": 500,
        "currency": "usd",
    })
    with patch("src.gateway.stripe_webhook.verify_webhook_event", return_value=event), \
         patch("src.gateway.stripe_webhook._credit_wallet") as mock_credit, \
         patch("src.gateway.stripe_webhook._record_topup") as mock_record, \
         patch("src.gateway.stripe_webhook._already_processed", return_value=False), \
         patch("src.gateway.stripe_webhook.get_db"):
        r = client.post("/webhook/stripe", data=b"{}",
                        headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 200
    mock_credit.assert_called_once_with("u_test", 500.0)
    mock_record.assert_called_once()


def test_webhook_idempotent(client):
    event = _fake_event("checkout.session.completed", {
        "id": "cs_1", "payment_intent": "pi_1",
        "client_reference_id": "u_test",
        "metadata": {"user_id": "u_test", "package_id": "p1", "credits": "500"},
    })
    with patch("src.gateway.stripe_webhook.verify_webhook_event", return_value=event), \
         patch("src.gateway.stripe_webhook._already_processed", return_value=True), \
         patch("src.gateway.stripe_webhook._credit_wallet") as mock_credit, \
         patch("src.gateway.stripe_webhook.get_db"):
        r = client.post("/webhook/stripe", data=b"{}",
                        headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 200
    mock_credit.assert_not_called()


def test_bad_signature_returns_400(client):
    import stripe
    with patch("src.gateway.stripe_webhook.verify_webhook_event",
               side_effect=stripe.error.SignatureVerificationError("bad", "sig")):
        r = client.post("/webhook/stripe", data=b"{}",
                        headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 400


def test_refund_debits(client):
    event = _fake_event("charge.refunded", {
        "id": "ch_1",
        "payment_intent": "pi_1",
        "amount_refunded": 500,
        "currency": "usd",
    })
    with patch("src.gateway.stripe_webhook.verify_webhook_event", return_value=event), \
         patch("src.gateway.stripe_webhook._lookup_topup_by_pi") as mock_lookup, \
         patch("src.gateway.stripe_webhook._debit_wallet_for_refund") as mock_debit, \
         patch("src.gateway.stripe_webhook.get_db"):
        mock_lookup.return_value = {"user_id": "u_test", "amount": 500}
        r = client.post("/webhook/stripe", data=b"{}",
                        headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 200
    mock_debit.assert_called_once_with("u_test", 500.0)

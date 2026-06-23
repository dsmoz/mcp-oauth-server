"""Thin wrapper around the Stripe SDK.

Env:
  STRIPE_SECRET_KEY        sk_test_* in test, sk_live_* in prod
  STRIPE_WEBHOOK_SECRET    whsec_* — used to verify webhook signatures
"""

from __future__ import annotations

import os

import stripe


class StripeNotConfigured(Exception):
    """Raised when required Stripe env vars are missing."""


def _configure() -> None:
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise StripeNotConfigured("STRIPE_SECRET_KEY not set")
    stripe.api_key = key


def create_checkout_session(
    *,
    user_id: str,
    package_id: str,
    unit_amount_cents: int,
    credits: float,
    success_url: str,
    cancel_url: str,
) -> dict:
    """Create a one-time Stripe Checkout Session for a top-up package.

    ``client_reference_id`` carries gateway user_id. ``metadata`` carries
    package_id + credits so the webhook handler can credit deterministically.
    """
    _configure()
    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": int(unit_amount_cents),
                "product_data": {"name": f"DS-MOZ scholar credits: {credits:g}"},
            },
            "quantity": 1,
        }],
        client_reference_id=user_id,
        metadata={
            "user_id": user_id,
            "package_id": package_id,
            "credits": str(credits),
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return {"session_id": session.id, "checkout_url": session.url}


def verify_webhook_event(payload: bytes, signature: str):
    """Verify and parse a Stripe webhook event.

    Raises ``stripe.error.SignatureVerificationError`` on bad signature.
    Returns a ``stripe.Event`` object.
    """
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET not set")
    return stripe.Webhook.construct_event(payload, signature, secret)

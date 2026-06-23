"""Stripe webhook handler.

Credits the user's wallet on ``checkout.session.completed``; debits it on
``charge.refunded``. Idempotent via the UNIQUE ``stripe_session_id`` column
on credit_topup_requests (added in Task 1).

Env:
  STRIPE_WEBHOOK_SECRET   whsec_* — used by verify_webhook_event.
"""

from __future__ import annotations

import logging
from typing import Optional

import stripe
from fastapi import APIRouter, Header, HTTPException, Request

from src.db import get_db
from src.gateway.stripe_client import StripeNotConfigured, verify_webhook_event

router = APIRouter(tags=["stripe"])
log = logging.getLogger(__name__)


def _already_processed(db, session_id: str) -> bool:
    row = (
        db.table("credit_topup_requests")
        .select("id")
        .eq("stripe_session_id", session_id)
        .limit(1)
        .execute()
    )
    return bool(row.data)


def _credit_wallet(user_id: str, credits: float) -> None:
    db = get_db()
    db.rpc("credit_wallet_user",
           {"p_user_id": user_id, "p_amount": credits}).execute()


def _debit_wallet_for_refund(user_id: str, credits: float) -> None:
    db = get_db()
    db.rpc("settle_credits_user",
           {"p_user_id": user_id, "p_amount": credits}).execute()


def _record_topup(
    db,
    *,
    user_id: str,
    package_id: str,
    credits: float,
    session_id: str,
    payment_intent: str,
    price_amount: Optional[float] = None,
    currency: Optional[str] = None,
) -> None:
    row = {
        "user_id": user_id,
        "amount": credits,
        "status": "auto_approved",
        "payment_provider": "stripe",
        "stripe_session_id": session_id,
        "stripe_payment_intent": payment_intent or None,
    }
    if package_id:
        # package_id is a uuid column; skip if empty
        row["package_id"] = package_id
    if price_amount is not None:
        row["price_amount"] = price_amount
    if currency:
        row["currency"] = currency.upper()
    db.table("credit_topup_requests").insert(row).execute()


def _lookup_topup_by_pi(db, payment_intent: str) -> Optional[dict]:
    row = (
        db.table("credit_topup_requests")
        .select("user_id, amount")
        .eq("stripe_payment_intent", payment_intent)
        .limit(1)
        .execute()
    )
    return row.data[0] if row.data else None


@router.post("/webhook/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(default=""),
) -> dict:
    raw = await request.body()
    try:
        event = verify_webhook_event(raw, stripe_signature)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="bad_signature")
    except StripeNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    etype = event["type"] if isinstance(event, dict) else event.type
    data = (event["data"]["object"] if isinstance(event, dict)
            else event.data.object)

    db = get_db()

    if etype == "checkout.session.completed":
        session_id = data["id"]
        if _already_processed(db, session_id):
            return {"status": "duplicate", "session_id": session_id}
        meta = data.get("metadata") or {}
        user_id = meta.get("user_id") or data.get("client_reference_id")
        try:
            credits = float(meta.get("credits", 0))
        except (TypeError, ValueError):
            credits = 0.0
        package_id = meta.get("package_id", "") or ""
        if not user_id or credits <= 0:
            log.error("stripe webhook missing user_id/credits: %s", data)
            raise HTTPException(status_code=400, detail="missing_metadata")

        amount_total = data.get("amount_total")
        price_amount = float(amount_total) / 100.0 if amount_total is not None else None

        _credit_wallet(user_id, credits)
        _record_topup(
            db,
            user_id=user_id,
            package_id=package_id,
            credits=credits,
            session_id=session_id,
            payment_intent=data.get("payment_intent", ""),
            price_amount=price_amount,
            currency=data.get("currency"),
        )
        return {"status": "ok", "credited": credits}

    if etype == "charge.refunded":
        pi = data.get("payment_intent", "")
        topup = _lookup_topup_by_pi(db, pi)
        if topup is None:
            log.warning("refund for unknown payment_intent %s", pi)
            return {"status": "ignored"}
        _debit_wallet_for_refund(topup["user_id"], float(topup["amount"]))
        return {"status": "refunded"}

    return {"status": "ignored", "type": etype}

"""Wallet API — credit balance and topup package endpoints.

Authenticated endpoints for viewing user wallet state and available topup packages.
Both require JWT authentication via current_jwt_user dependency.
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from starlette.requests import Request

from src.db import get_db
from src.gateway.jwt_auth import current_jwt_user
from src.gateway.billing import UsageMeta, compute_cost
from src.gateway.routes import _settle_credits
from src.gateway.stripe_client import StripeNotConfigured, create_checkout_session


router = APIRouter(prefix="/api/v1/wallet", tags=["wallet"])


class BalanceResponse(BaseModel):
    """User credit balance and USD equivalent."""
    balance_credits: float = Field(..., description="Credit balance in credits")
    balance_usd: float = Field(..., description="USD equivalent at current exchange rate")
    currency: str = Field(default="USD", description="Currency code (ISO 4217)")


class Package(BaseModel):
    """A published topup package available for purchase."""
    id: str
    name: str
    credits_granted: float
    price_usd: float


class PackagesResponse(BaseModel):
    """Published topup packages ordered by price."""
    packages: list[Package] = Field(..., description="Topup packages sorted by price (ascending)")


class CheckoutIn(BaseModel):
    package_id: str
    success_url: str
    cancel_url: str


class CheckoutOut(BaseModel):
    session_id: str
    checkout_url: str


def _usd_per_credit(db) -> float:
    """Fetch current USD-per-credit exchange rate from pricing_config.

    Falls back to 0.01 if not configured (standard rate).
    """
    result = (
        db.table("pricing_config")
        .select("usd_per_credit")
        .eq("id", 1)
        .limit(1)
        .execute()
    )
    return float(result.data[0]["usd_per_credit"]) if result.data else 0.01


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(user_id: str = Depends(current_jwt_user), db = Depends(get_db)):
    """Get user credit balance and USD equivalent.

    Requires JWT authentication.
    """
    # Fetch user credit balance
    user_result = (
        db.table("users")
        .select("credit_balance")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not user_result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="user not found"
        )

    credit_balance = float(user_result.data[0]["credit_balance"])
    usd_per_credit = _usd_per_credit(db)
    usd_balance = credit_balance * usd_per_credit

    return BalanceResponse(
        balance_credits=credit_balance,
        balance_usd=usd_balance,
        currency="USD"
    )


@router.get("/packages", response_model=PackagesResponse)
async def get_packages(user_id: str = Depends(current_jwt_user), db = Depends(get_db)):
    """Get published topup packages ordered by price.

    Requires JWT authentication. Only USD-priced packages are exposed in this v1 endpoint.
    """
    result = (
        db.table("topup_packages")
        .select("id, name, price_amount, credits, currency, is_published")
        .eq("is_published", True)
        .order("price_amount", desc=False)
        .execute()
    )

    packages = []
    for row in result.data or []:
        # Only USD-priced packages are exposed in this v1 endpoint
        if (row.get("currency") or "USD").upper() != "USD":
            continue
        packages.append(Package(
            id=row["id"],
            name=row["name"],
            price_usd=float(row["price_amount"]),
            credits_granted=float(row["credits"]),
        ))

    return PackagesResponse(packages=packages)


class DebitUsageIn(BaseModel):
    """Optional LLM usage from the scholar BFF."""
    usage_usd: Optional[float] = None
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


class DebitIn(BaseModel):
    """Request body for /wallet/debit endpoint."""
    mcp_slug: str = Field(default="mcp-scholar-bff", description="MCP identifier (default: mcp-scholar-bff)")
    duration_ms: int = Field(default=0, description="Execution duration in milliseconds")
    response_bytes: int = Field(default=0, description="Response size in bytes")
    usage: DebitUsageIn = Field(default_factory=DebitUsageIn, description="Optional LLM usage metrics")
    request_id: str = Field(..., description="Unique idempotency key for this call")


class DebitOut(BaseModel):
    """Response body for /wallet/debit endpoint."""
    new_balance_credits: float = Field(..., description="Credit balance after settlement")
    sell_usd: float = Field(..., description="USD charged to the user (sell price)")
    raw_usd: float = Field(..., description="Raw cost before margin (cost price)")
    credits_charged: float = Field(..., description="Number of credits deducted")
    idempotent: bool = Field(default=False, description="True if this was a duplicate call")


def _already_logged(db, request_id: str) -> bool:
    """Check if this request_id was already logged in oauth_usage_logs."""
    row = (
        db.table("oauth_usage_logs")
        .select("request_id")
        .eq("request_id", request_id)
        .limit(1)
        .execute()
    )
    return bool(row.data)


def _write_usage_log(
    db,
    user_id: str,
    mcp_slug: str,
    request_id: str,
    breakdown,
    duration_ms: int,
    response_bytes: int,
) -> None:
    """Write a usage record to oauth_usage_logs after billing."""
    db.table("oauth_usage_logs").insert({
        "user_id": user_id,
        "mcp_slug": mcp_slug,
        "request_id": request_id,
        "caller_kind": "scholar_bff",
        "duration_ms": duration_ms,
        "response_bytes": response_bytes,
        "compute_usd": breakdown.compute_usd,
        "llm_usd": breakdown.llm_usd,
        "raw_usd": breakdown.raw_usd,
        "sell_usd": breakdown.sell_usd,
        "credits_charged": breakdown.credits_charged,
        "model_used": breakdown.model_used,
        "input_tokens": breakdown.input_tokens,
        "output_tokens": breakdown.output_tokens,
    }).execute()


@router.post("/debit", response_model=DebitOut)
def post_debit(
    payload: DebitIn,
    user_id: str = Depends(current_jwt_user),
) -> DebitOut:
    """Bill scholar BFF usage by deducting credits.

    Accepts usage metrics from the scholar BFF, computes cost via the existing
    compute_cost pipeline, settles the charge atomically, and logs to oauth_usage_logs.

    Idempotent on request_id: a duplicate call returns the current balance without
    billing or logging again (backed by UNIQUE partial index on oauth_usage_logs.request_id).

    Requires JWT authentication.
    """
    db = get_db()

    # Idempotent path: already logged this request_id
    if _already_logged(db, payload.request_id):
        row = (
            db.table("users")
            .select("credit_balance")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        bal = float(row.data[0]["credit_balance"] or 0) if row.data else 0.0
        return DebitOut(
            new_balance_credits=bal,
            sell_usd=0.0,
            raw_usd=0.0,
            credits_charged=0.0,
            idempotent=True,
        )

    # Compute cost using the gateway's standard pipeline
    breakdown = compute_cost(
        mcp_slug=payload.mcp_slug,
        duration_ms=payload.duration_ms,
        response_bytes=payload.response_bytes,
        usage=UsageMeta(
            usage_usd=payload.usage.usage_usd,
            model=payload.usage.model,
            input_tokens=payload.usage.input_tokens,
            output_tokens=payload.usage.output_tokens,
            cached_input_tokens=payload.usage.cached_input_tokens,
        ),
    )

    # Settle credits atomically
    status_str, new_balance = _settle_credits(user_id, breakdown.credits_charged)
    if status_str != "ok":
        raise HTTPException(status_code=502, detail="billing_settle_failed")

    # Log the usage
    _write_usage_log(
        db, user_id, payload.mcp_slug, payload.request_id,
        breakdown, payload.duration_ms, payload.response_bytes,
    )

    return DebitOut(
        new_balance_credits=new_balance or 0.0,
        sell_usd=breakdown.sell_usd,
        raw_usd=breakdown.raw_usd,
        credits_charged=breakdown.credits_charged,
    )


@router.post("/checkout", response_model=CheckoutOut)
def post_checkout(
    payload: CheckoutIn,
    user_id: str = Depends(current_jwt_user),
) -> CheckoutOut:
    """Create a Stripe Checkout Session from a topup package.

    POST body: package_id, success_url, cancel_url
    Returns: session_id, checkout_url

    Looks up the published USD topup package from topup_packages table.
    Returns 404 on unknown/unpublished package.
    Returns 400 if package currency is not USD (v1 restriction).
    Returns 503 if Stripe is not configured.

    Requires JWT authentication.
    """
    db = get_db()
    row = (
        db.table("topup_packages")
        .select("id, name, price_amount, credits, currency, is_published")
        .eq("id", payload.package_id)
        .limit(1)
        .execute()
    )
    if not row.data or not row.data[0].get("is_published"):
        raise HTTPException(status_code=404, detail="package_not_found")
    pkg = row.data[0]

    currency = (pkg.get("currency") or "USD").upper()
    if currency != "USD":
        # v1 only supports USD packages via Stripe Checkout.
        raise HTTPException(status_code=400, detail="package_currency_unsupported")

    try:
        session = create_checkout_session(
            user_id=user_id,
            package_id=pkg["id"],
            unit_amount_cents=int(round(float(pkg["price_amount"]) * 100)),
            credits=float(pkg["credits"]),
            success_url=payload.success_url,
            cancel_url=payload.cancel_url,
        )
    except StripeNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return CheckoutOut(**session)

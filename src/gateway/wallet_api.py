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

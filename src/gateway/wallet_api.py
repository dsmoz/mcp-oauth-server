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
    credit_balance: float = Field(..., description="Credit balance in credits")
    usd_balance: float = Field(..., description="USD equivalent at current exchange rate")


class TopupPackage(BaseModel):
    """A published topup package available for purchase."""
    id: str
    name: str
    credit_amount: float
    usd_price: float


class PackagesResponse(BaseModel):
    """Published topup packages ordered by price."""
    packages: list[TopupPackage] = Field(..., description="Topup packages sorted by price (ascending)")


def _usd_per_credit(db) -> float:
    """Fetch current USD-per-credit exchange rate from pricing_config."""
    result = (
        db.table("pricing_config")
        .select("usd_per_credit")
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="pricing_config not found"
        )
    return float(result.data[0]["usd_per_credit"])


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
        credit_balance=credit_balance,
        usd_balance=usd_balance
    )


@router.get("/packages", response_model=PackagesResponse)
async def get_packages(user_id: str = Depends(current_jwt_user), db = Depends(get_db)):
    """Get published topup packages ordered by price.

    Requires JWT authentication.
    """
    result = (
        db.table("topup_packages")
        .select("id, name, credit_amount, usd_price")
        .eq("is_published", True)
        .order("usd_price", desc=False)
        .execute()
    )

    packages = [
        TopupPackage(
            id=row["id"],
            name=row["name"],
            credit_amount=float(row["credit_amount"]),
            usd_price=float(row["usd_price"]),
        )
        for row in result.data or []
    ]

    return PackagesResponse(packages=packages)

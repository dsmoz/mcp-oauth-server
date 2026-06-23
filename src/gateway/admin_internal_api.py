# Internal admin service API for the scholar-web BFF.
#
# Lets the scholar-web admin panel grant credits and read usage history for a
# Scholar user WITHOUT that user being signed in. The join key is the Scholar
# Supabase user UUID (supabase_user_id), resolved to a gateway users.user_id via
# resolve_or_create_user — the same mapping the per-user JWT path uses.
#
# Auth: the shared service secret (X-Service-Secret), the SAME server-to-server
# channel as /api/internal/scholar-link. scholar-web enforces its own
# requireAdmin() before calling; this layer only proves the caller is the
# scholar-web backend, not a browser. Never exposed to clients.
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.db import get_db
from src.gateway.jwt_auth import resolve_or_create_user
from src.gateway.wallet_api import _already_logged  # shared idempotency check
from src.portal.routes import _require_service_secret  # shared X-Service-Secret check
from src.users.provider import SupabaseUserProvider

router = APIRouter(
    prefix="/api/internal/admin",
    dependencies=[Depends(_require_service_secret)],
)


class GrantCreditsBody(BaseModel):
    supabase_user_id: str = Field(..., min_length=1)
    email: str = ""
    amount: float = Field(..., gt=0)
    reason: str = "admin/grant"
    admin_email: str = ""
    request_id: str = Field(..., min_length=1, description="Idempotency key")


@router.post("/credits")
async def grant_credits(body: GrantCreditsBody):
    """Add credits to a Scholar user's gateway wallet and audit-log the grant.

    Body: {supabase_user_id, email?, amount>0, reason?, admin_email?, request_id}.
    Returns {user_id, new_balance_credits, credited, idempotent}.

    Idempotent on request_id: a duplicate call (e.g. a network retry) returns the
    current balance without granting again, backed by the UNIQUE partial index on
    oauth_usage_logs.request_id.
    """
    db = get_db()
    gw_user_id = resolve_or_create_user(db, body.supabase_user_id, body.email)
    users = SupabaseUserProvider()

    # Idempotent path: this request_id already granted.
    if _already_logged(db, body.request_id):
        user = users.get_user(gw_user_id)
        return {
            "user_id": gw_user_id,
            "new_balance_credits": float(user.credit_balance) if user else 0.0,
            "credited": 0.0,
            "idempotent": True,
        }

    users.add_credits(gw_user_id, body.amount)
    user = users.get_user(gw_user_id)
    new_balance = float(user.credit_balance) if user else 0.0

    # Audit row — mirrors the signup-grant shape (credits_used negative = credit)
    # plus request_id. client_id marks the source; endpoint carries the granting
    # admin for attribution without polluting model_used (cost analytics group on
    # that). The UNIQUE index on request_id also locks out a racing duplicate.
    base = body.reason or "admin/grant"
    endpoint = f"{base}:{body.admin_email}" if body.admin_email else base
    try:
        db.table("oauth_usage_logs").insert({
            "user_id": gw_user_id,
            "client_id": "scholar-admin",
            "credits_used": -float(body.amount),
            "endpoint": endpoint,
            "request_id": body.request_id,
        }).execute()
    except Exception as exc:  # audit failure must not void the grant
        import sys
        print(f"WARNING: admin grant log failed for {gw_user_id}: {exc}", file=sys.stderr)

    return {
        "user_id": gw_user_id,
        "new_balance_credits": new_balance,
        "credited": float(body.amount),
        "idempotent": False,
    }


@router.get("/usage")
async def usage_history(
    supabase_user_id: str = Query(..., min_length=1),
    email: str = "",
    limit: int = Query(50, ge=1, le=500),
):
    """Recent usage/billing rows + current balance for a Scholar user.

    Returns {user_id, balance_credits, items:[{created_at, mcp_slug, endpoint,
    model_used, input_tokens, output_tokens, credits_charged, credits_used,
    sell_usd}]}.
    """
    db = get_db()
    gw_user_id = resolve_or_create_user(db, supabase_user_id, email)

    user = SupabaseUserProvider().get_user(gw_user_id)
    balance = float(user.credit_balance) if user else 0.0

    rows = (
        db.table("oauth_usage_logs")
        .select(
            "created_at, mcp_slug, endpoint, model_used, input_tokens, "
            "output_tokens, credits_charged, credits_used, sell_usd"
        )
        .eq("user_id", gw_user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    return {
        "user_id": gw_user_id,
        "balance_credits": balance,
        "items": rows.data or [],
    }

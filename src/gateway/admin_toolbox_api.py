# Internal admin API for the scholar-web BFF to auto-provision the Scholar
# entry in a user's gateway toolbox.
#
# Flow: when a Scholar user flips the "Enable MCP" toggle, scholar-web mints a
# fresh sch_live_* token (scope=mcp) and POSTs it here. We resolve the gateway
# user_id by email, ensure "scholar" is in users.allowed_mcp_resources, and
# upsert client_mcp_credentials so the gateway proxy can inject the token on
# upstream calls. Mirror endpoint removes both on disable.
#
# Auth: shared X-Service-Secret — same channel as /api/internal/scholar-link
# and the credit-admin API. Server-to-server only.
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.db import get_db
from src.portal.routes import _require_service_secret  # shared X-Service-Secret check
from src.users.agent_tokens import AgentTokenProvider
from src.users.provider import SupabaseUserProvider

router = APIRouter(
    prefix="/api/internal/admin/toolbox",
    dependencies=[Depends(_require_service_secret)],
)


def _resolve_user_id_by_email(users: SupabaseUserProvider, email: str) -> str:
    email_lc = email.strip().lower()
    if not email_lc:
        raise HTTPException(status_code=400, detail="email required")
    user = users.get_user_by_email(email_lc)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown email")
    return user.user_id


class AutoAddBody(BaseModel):
    email: str = Field(..., min_length=1)
    mcp_slug: str = Field(..., min_length=1)
    upstream_token: str = Field(..., min_length=1)


@router.post("/auto_add")
async def auto_add(body: AutoAddBody):
    """Enable an MCP slug in the user's toolbox and store its upstream credential.

    Idempotent: re-calling rotates the credential (overwrites api_token) and is a
    no-op on allowed_mcp_resources if already present.
    """
    users = SupabaseUserProvider()
    user_id = _resolve_user_id_by_email(users, body.email)

    # 1. Ensure slug is in users.allowed_mcp_resources.
    user = users.get_user(user_id)
    current = list(user.allowed_mcp_resources or []) if user else []
    if body.mcp_slug not in current:
        users.set_allowed_mcps(user_id, current + [body.mcp_slug])

    # 2. Upsert the upstream credential. Schema is {api_token: <sch_live_…>}.
    db = get_db()
    db.table("client_mcp_credentials").upsert(
        {
            "user_id": user_id,
            "mcp_slug": body.mcp_slug,
            "credentials": {"api_token": body.upstream_token},
        },
        on_conflict="user_id,mcp_slug",
    ).execute()

    return {"user_id": user_id, "added": True, "mcp_slug": body.mcp_slug}


class AutoRemoveBody(BaseModel):
    email: str = Field(..., min_length=1)
    mcp_slug: str = Field(..., min_length=1)


@router.post("/auto_remove")
async def auto_remove(body: AutoRemoveBody):
    """Remove the slug from the user's toolbox and delete the stored credential.

    Idempotent: missing entries are silently ignored.
    """
    users = SupabaseUserProvider()
    user_id = _resolve_user_id_by_email(users, body.email)

    user = users.get_user(user_id)
    current = list(user.allowed_mcp_resources or []) if user else []
    if body.mcp_slug in current:
        users.set_allowed_mcps(
            user_id, [s for s in current if s != body.mcp_slug]
        )

    db = get_db()
    db.table("client_mcp_credentials").delete().eq("user_id", user_id).eq(
        "mcp_slug", body.mcp_slug
    ).execute()

    return {"user_id": user_id, "removed": True, "mcp_slug": body.mcp_slug}


class CheckBody(BaseModel):
    email: str = Field(..., min_length=1)
    mcp_slug: str = Field(..., min_length=1)


@router.post("/check")
async def check(body: CheckBody):
    """Report current enable state — whether the slug is in allowed list and
    whether a credential row exists."""
    users = SupabaseUserProvider()
    user_id = _resolve_user_id_by_email(users, body.email)
    user = users.get_user(user_id)
    allowed = body.mcp_slug in (user.allowed_mcp_resources or []) if user else False

    db = get_db()
    cred_rows = (
        db.table("client_mcp_credentials")
        .select("user_id")
        .eq("user_id", user_id)
        .eq("mcp_slug", body.mcp_slug)
        .limit(1)
        .execute()
    ).data
    return {
        "user_id": user_id,
        "enabled": bool(allowed),
        "has_credential": bool(cred_rows),
    }


# ── Agent-token mint (Scholar plugin pairing) ────────────────────────────────
# Separate prefix because the path category differs from /toolbox/* but the
# auth surface (X-Service-Secret) is shared.

agent_tokens_router = APIRouter(
    prefix="/api/internal/admin/agent_tokens",
    dependencies=[Depends(_require_service_secret)],
)


class MintAgentTokenBody(BaseModel):
    email: str = Field(..., min_length=1)
    label: str | None = Field(None, max_length=120)


@agent_tokens_router.post("/mint")
async def mint_agent_token(body: MintAgentTokenBody):
    """Mint a dsmoz_* personal connection token for the gateway user identified
    by email. Used by scholar-web's plugin pair-approve route so the issued
    token is one the gateway already recognises on /api/plugin/*.

    Returns 404 if no gateway user has that email — scholar-web surfaces a
    "create a Connect account first" message in that case.
    """
    users = SupabaseUserProvider()
    user_id = _resolve_user_id_by_email(users, body.email)
    raw, _ = AgentTokenProvider().create(
        user_id=user_id, label=body.label or "Zotero plugin (Scholar pairing)"
    )
    return {"user_id": user_id, "token": raw}

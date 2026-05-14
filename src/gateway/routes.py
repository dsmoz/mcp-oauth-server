"""
Gateway — one MCP endpoint per user (tenant) via Streamable HTTP transport.

GET/POST/DELETE  /gateway/{user_id}       — primary endpoint
GET/POST/DELETE  /gateway/{user_id}/mcp   — alias with /mcp suffix
GET/POST/DELETE  /gateway/me              — token-resolved alias (same user_id as token)
GET/POST/DELETE  /gateway/me/mcp          — alias with /mcp suffix

The path key is the user_id (tenant). Access tokens carry both user_id and
client_id: the user_id must match the URL, and the client_id is forwarded
upstream only as telemetry via X-Client-ID. Upstream MCPs namespace
per-tenant state on X-User-ID.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import anyio

from fastapi.responses import JSONResponse
from starlette.requests import Request
from mcp.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp import types

from src.cache import TTLCache
from src.config import get_settings
from src.db import get_db
from src.gateway.upstream import (
    _walk_exceptions,
    call_upstream_tool,
    call_upstream_tool_structured,
    fetch_tool_list,
    list_upstream_resources,
    read_upstream_resource,
    TOOL_CALL_TIMEOUT,
)


def _flatten_exception_message(exc: BaseException) -> str:
    """Return the most informative leaf message from a (possibly nested
    ExceptionGroup / chained) exception, instead of the opaque
    "unhandled errors in a TaskGroup (1 sub-exception)" wrapper."""
    leaves: list[str] = []
    for e in _walk_exceptions(exc):
        if isinstance(e, BaseExceptionGroup):
            continue
        msg = str(e).strip()
        if msg:
            leaves.append(f"{type(e).__name__}: {msg}")
    if not leaves:
        return f"{type(exc).__name__}: {exc}"
    # Deduplicate while preserving order; cap to keep payload bounded.
    seen: set[str] = set()
    uniq: list[str] = []
    for m in leaves:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return " | ".join(uniq[:5])
from src.oauth.provider import SupabaseOAuthProvider
from src.users.agent_tokens import AgentTokenProvider


def _ensure_agent_client(user_id: str) -> str:
    """Find-or-create a default oauth_clients row for agent-token traffic.
    Returns client_id used for usage logging only — agent tokens authenticate
    independently and do not need a client_secret.
    """
    from datetime import datetime, timezone
    from src.crypto import generate_client_id, generate_token, hash_secret
    db = get_db()
    existing = (
        db.table("oauth_clients")
        .select("client_id")
        .eq("user_id", user_id)
        .eq("client_name", "dsmoz agent")
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]["client_id"]
    client_id = generate_client_id()
    db.table("oauth_clients").insert({
        "client_id": client_id,
        "client_secret_hash": hash_secret(generate_token(32)),
        "client_name": "dsmoz agent",
        "redirect_uris": [],
        "grant_types": ["authorization_code"],
        "scope": "mcp",
        "created_by": "agent-token",
        "is_active": True,
        "user_id": user_id,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return client_id


# ── Module-level caches ──────────────────────────────────────────────────────
# Hot DB lookups (credit cost, published catalogue) get short TTLs; the
# per-(slug,user_id) upstream tool descriptor list gets a 5-minute TTL so it
# survives across gateway connections.

_credit_cost_cache: TTLCache[str, float] = TTLCache(ttl=300, maxsize=256)
_published_mcps_cache: TTLCache[str, list[dict]] = TTLCache(ttl=60, maxsize=4)
_tool_cache: TTLCache[tuple[str, str], list[dict]] = TTLCache(ttl=300, maxsize=4096)
# Per-user MCP credential config — short TTL; invalidated on portal save.
_user_config_cache: TTLCache[tuple[str, str], dict] = TTLCache(ttl=60, maxsize=4096)


def _invalidate_user_tool_cache(user_id: str) -> None:
    """Drop every (slug, user_id) entry for this user. Called on add/remove_mcp."""
    keys = [k for k in list(_tool_cache._store.keys()) if k[1] == user_id]
    for k in keys:
        _tool_cache.pop(k)


def _invalidate_user_config_cache(user_id: str, slug: str | None = None) -> None:
    """Drop cached user MCP config. If slug given, drop only that entry; else all for user."""
    if slug is not None:
        _user_config_cache.pop((user_id, slug))
        # Also drop tool cache entry — config change may affect what tools surface.
        _tool_cache.pop((slug, user_id))
        return
    keys = [k for k in list(_user_config_cache._store.keys()) if k[0] == user_id]
    for k in keys:
        _user_config_cache.pop(k)


def _load_user_mcp_config(user_id: str, slug: str) -> dict:
    """Return the user's saved per-MCP credential config (60s TTL). Empty dict if none."""
    cache_key = (user_id, slug)
    cached = _user_config_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        row = (
            get_db().table("user_mcp_configs")
            .select("config")
            .eq("user_id", user_id)
            .eq("mcp_slug", slug)
            .limit(1)
            .execute()
        )
        cfg = (row.data or [{}])[0].get("config") or {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception as exc:
        print(f"WARNING: _load_user_mcp_config failed {user_id}/{slug}: {exc}", file=sys.stderr)
        cfg = {}
    _user_config_cache.set(cache_key, cfg)
    return cfg


def _user_config_headers(config_schema, user_config: dict) -> dict[str, str]:
    """Pack saved config values into a single `X-MCP-Credentials` header.

    The header value is base64-encoded JSON of the field-key → value dict.
    Schema keys must match the upstream MCP's internal credential field names
    (the MCP merges this dict directly into its credential config).

    Returns {} when schema is empty, user has saved no values, or all
    values are blank — so the header is omitted entirely and the MCP falls
    back to its env-var defaults.
    """
    if not config_schema or not isinstance(config_schema, list) or not user_config:
        return {}
    payload: dict[str, str] = {}
    for field in config_schema:
        if not isinstance(field, dict):
            continue
        key = field.get("key")
        if not key or not isinstance(key, str):
            continue
        val = user_config.get(key)
        if val is None or val == "":
            continue
        payload[key] = str(val)
    if not payload:
        return {}
    import base64 as _b64, json as _json
    encoded = _b64.b64encode(_json.dumps(payload).encode()).decode()
    return {"X-MCP-Credentials": encoded}


def _extra_headers_for(mcp: dict, user_id: str) -> dict[str, str]:
    """Combined per-MCP user-credential headers. Returns {} when MCP has no config_schema."""
    schema = mcp.get("config_schema")
    if not schema:
        return {}
    cfg = _load_user_mcp_config(user_id, mcp["slug"])
    return _user_config_headers(schema, cfg)


GATEWAY_INSTRUCTIONS = """\
DS-MOZ Connect Gateway — a multi-tenant proxy in front of many upstream
MCP servers. Each authenticated user has a persistent "toolbox" of enabled
MCPs. You, the assistant, discover, enable, search and invoke tools from
those upstream MCPs through the seven meta-tools exposed here.

Typical lifecycle:
  1. browse_mcps              → see the full catalogue (with enabled flag + cost)
  2. add_mcp(mcp_slug=...)    → persist an MCP into the user's toolbox
  3. search_tools(query=...)  → keyword search across all enabled MCPs
     (or list_mcp_tools if you already know which MCP you want)
  4. invoke_mcp_tool(...)     → run a real tool on an upstream MCP

The toolbox persists across sessions — on later calls you usually start at
search_tools/list_mcps without re-adding anything.

Credits:
  • All six meta-tools (list/browse/add/remove/search/list_mcp_tools) are FREE.
  • invoke_mcp_tool is CREDIT-GATED — each call deducts credit_cost_per_call
    (see browse_mcps / list_mcps). On insufficient balance the call returns
    {"error": "Insufficient credits..."} without hitting the upstream.
  • If the user runs out, direct them to /portal/credits to top up.

Return shape: all tools return a single TextContent whose .text is a JSON
string. Parse it as JSON. Errors come back as {"error": "..."}.
"""


def evict_transport(client_id: str) -> None:
    """No-op — kept for call-site compatibility. Streamable HTTP is stateless."""
    pass


async def start_cleanup_loop() -> None:
    """No-op — kept for main.py compatibility. No persistent transports to clean up."""
    while True:
        await asyncio.sleep(3600)


def _resolve_user_id_for_token(at) -> str | None:
    """Extract user_id from an access token row, falling back to the owning
    oauth_client's user_id for legacy tokens that predate the users table."""
    user_id = getattr(at, "user_id", None)
    if user_id:
        return user_id
    try:
        row = (
            get_db()
            .table("oauth_clients")
            .select("user_id")
            .eq("client_id", at.client_id)
            .limit(1)
            .execute()
        )
        if row.data and row.data[0].get("user_id"):
            return row.data[0]["user_id"]
    except Exception:
        pass
    return None


def _load_enabled_mcps(user_id: str) -> list[dict]:
    """Return MCPs the user has added, filtered to published and tier-allowed ones."""
    db = get_db()
    user_row = (
        db.table("users")
        .select("allowed_mcp_resources, tier")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not user_row.data:
        return []
    row = user_row.data[0]
    slugs = row.get("allowed_mcp_resources") or []
    if not slugs:
        return []
    user_tier = row.get("tier") or "standard"
    query = (
        db.table("mcp_catalogue")
          .select("*")
          .in_("slug", slugs)
          .eq("is_published", True)
    )
    if user_tier != "super":
        query = query.eq("tier", "standard")
    return query.execute().data or []


def _get_credit_cost(mcp_slug: str) -> float:
    """Return credit_cost_per_call for an MCP slug (0 if not set).

    Cached for 300s; catalogue cost changes are rare.
    """
    cached = _credit_cost_cache.get(mcp_slug)
    if cached is not None:
        return cached
    try:
        row = get_db().table("mcp_catalogue").select("credit_cost_per_call").eq("slug", mcp_slug).limit(1).execute()
        cost = float((row.data or [{}])[0].get("credit_cost_per_call") or 0)
    except Exception:
        return 0.0
    _credit_cost_cache.set(mcp_slug, cost)
    return cost


def _deduct_credits(user_id: str, amount: float) -> float:
    """Atomically deduct credits from a user via Supabase RPC. Returns new balance or -1 if insufficient."""
    try:
        result = get_db().rpc(
            "deduct_credits_user", {"p_user_id": user_id, "p_amount": amount}
        ).execute()
        return float(result.data) if result.data is not None else -1
    except Exception as exc:
        print(f"WARNING: credit deduction failed for user {user_id}: {exc}", file=sys.stderr)
        return -1


def _log_tool_call(
    user_id: str, client_id: str, mcp_slug: str, tool_name: str,
    credits_used: float = 0.0, duration_ms: int | None = None,
    response_bytes: int | None = None,
) -> None:
    try:
        row: dict = {
            "user_id": user_id,
            "client_id": client_id,
            "endpoint": f"gateway/{mcp_slug}/{tool_name}",
            "credits_used": credits_used,
        }
        if duration_ms is not None:
            row["duration_ms"] = duration_ms
        if response_bytes is not None:
            row["response_bytes"] = response_bytes
        get_db().table("oauth_usage_logs").insert(row).execute()
    except Exception as exc:
        print(f"WARNING: usage log failed for user {user_id}: {exc}", file=sys.stderr)


_user_tier_cache: TTLCache[str, str] = TTLCache(ttl=60, maxsize=1024)


def _get_user_tier(user_id: str) -> str:
    cached = _user_tier_cache.get(user_id)
    if cached is not None:
        return cached
    row = get_db().table("users").select("tier").eq("user_id", user_id).limit(1).execute()
    tier = (row.data or [{}])[0].get("tier") or "standard"
    _user_tier_cache.set(user_id, tier)
    return tier


def _get_all_published_mcps(user_tier: str = "standard") -> list[dict]:
    """Cached snapshot of the published catalogue (60s TTL), keyed by tier.

    Acceptable staleness for admin catalogue mutations — operators who toggle
    publish flags or tier filters will see the change within a minute.
    """
    cache_key = "super" if user_tier == "super" else "standard"
    cached = _published_mcps_cache.get(cache_key)
    if cached is not None:
        return cached
    query = get_db().table("mcp_catalogue").select("*").eq("is_published", True)
    if user_tier != "super":
        query = query.eq("tier", "standard")
    rows = query.execute().data or []
    _published_mcps_cache.set(cache_key, rows)
    return rows


def _log_tool_call_async(
    user_id: str, client_id: str, mcp_slug: str, tool_name: str,
    credits_used: float = 0.0, duration_ms: int | None = None,
    response_bytes: int | None = None,
) -> None:
    """Fire-and-forget wrapper for _log_tool_call.

    Insert happens in a background thread so the synchronous Supabase HTTP
    write doesn't sit on the request path. Exceptions are swallowed and logged.
    """
    async def _runner() -> None:
        try:
            await asyncio.to_thread(
                _log_tool_call, user_id, client_id, mcp_slug, tool_name,
                credits_used, duration_ms, response_bytes,
            )
        except Exception as exc:
            print(f"WARNING: background usage log failed for user {user_id}: {exc}", file=sys.stderr)
    try:
        asyncio.get_running_loop().create_task(_runner())
    except RuntimeError:
        # No running loop (shouldn't happen in handler context) — fall back to sync.
        _log_tool_call(
            user_id, client_id, mcp_slug, tool_name,
            credits_used, duration_ms, response_bytes,
        )


def _update_user_mcps(user_id: str, slugs: list[str]) -> None:
    get_db().table("users").update(
        {"allowed_mcp_resources": slugs}
    ).eq("user_id", user_id).execute()


def _tool_has_ui(tool: dict) -> bool:
    """Detect whether a tool descriptor carries an MCP Apps UI pointer."""
    meta = tool.get("_meta") or {}
    if not isinstance(meta, dict):
        return False
    ui = meta.get("ui")
    if isinstance(ui, dict) and ui.get("resourceUri"):
        return True
    # OpenAI-style hint some servers emit
    openai = meta.get("openai")
    if isinstance(openai, dict) and openai.get("outputTemplate"):
        return True
    return False


_MCP_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "Stable identifier for the MCP (use this as mcp_slug everywhere)."},
        "name": {"type": "string", "description": "Human-readable MCP name."},
        "description": {"type": "string", "description": "What the MCP does."},
        "category": {"type": "string", "description": "Catalogue category (research, productivity, ...)."},
        "credit_cost_per_call": {"type": "number", "description": "Credits deducted per invoke_mcp_tool call against this MCP."},
    },
}

_UPSTREAM_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "inputSchema": {"type": "object"},
    },
}

_MUTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["added", "removed", "already_enabled"]},
        "mcp": {"type": "string"},
        "name": {"type": "string"},
        "error": {"type": "string"},
    },
}


def _build_mcp_server(user_id: str, client_id: str, enabled_mcps: list[dict]) -> Server:
    mcp_by_slug = {m["slug"]: m for m in enabled_mcps}
    # Promoted UI tools: promoted_name -> {slug, upstream_name, descriptor}
    _ui_tools: dict[str, dict] = {}
    # Resource origins: uri -> slug (populated lazily by list_resources_handler)
    _resource_origin: dict[str, str] = {}

    server = Server(
        "DS-MOZ Connect Gateway",
        instructions=GATEWAY_INSTRUCTIONS,
    )

    @server.list_tools()
    async def list_tools_handler() -> list[types.Tool]:
        # Note: outputSchema is passed through Tool's `extra="allow"` pydantic config.
        # Deprecated aliases (list_tools, call_tool) are intentionally not advertised
        # here — they remain dispatchable in call_tool_handler for one release cycle.
        return [
            types.Tool(
                name="list_mcps",
                description=(
                    "List MCPs currently enabled in the caller's toolbox.\n\n"
                    "When to use: you need to know which upstream MCPs are active "
                    "right now before calling their tools. Contrast with `browse_mcps`, "
                    "which also shows MCPs the user hasn't enabled yet.\n\n"
                    "Returns: JSON array of {slug, name, description, category, "
                    "credit_cost_per_call}. Empty array means the toolbox is empty "
                    "— call `browse_mcps` then `add_mcp` to populate it.\n\n"
                    "Credit cost: free."
                ),
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                outputSchema={
                    "type": "object",
                    "properties": {"items": {"type": "array", "items": _MCP_ENTRY_SCHEMA}},
                    "required": ["items"],
                },
            ),
            types.Tool(
                name="browse_mcps",
                description=(
                    "Browse the full published catalogue of MCPs, including ones "
                    "the user has not yet enabled.\n\n"
                    "When to use: the caller asks 'what can I do here?' or you need "
                    "to find an MCP by capability before enabling it. Each entry "
                    "carries an `enabled` flag and its `credit_cost_per_call` so you "
                    "can budget before invoking.\n\n"
                    "Returns: JSON array of {slug, name, description, category, "
                    "enabled, credit_cost_per_call}.\n\n"
                    "Credit cost: free."
                ),
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                outputSchema={
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "allOf": [
                                    _MCP_ENTRY_SCHEMA,
                                    {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
                                ]
                            },
                        }
                    },
                    "required": ["items"],
                },
            ),
            types.Tool(
                name="add_mcp",
                description=(
                    "Enable an MCP in the caller's toolbox so its tools become "
                    "reachable via `search_tools` / `list_mcp_tools` / `invoke_mcp_tool`. "
                    "Persists across sessions.\n\n"
                    "When to use: after `browse_mcps` surfaces an MCP the caller "
                    "wants to use. Idempotent — returns status='already_enabled' if "
                    "the slug is already in the toolbox.\n\n"
                    "Returns: {status: 'added'|'already_enabled', mcp, name} or "
                    "{error: '...'} if the slug is unknown/unpublished.\n\n"
                    "Credit cost: free."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mcp_slug": {
                            "type": "string",
                            "description": "Slug from `browse_mcps` (e.g. 'dsmoz-intel'). Case-sensitive.",
                        }
                    },
                    "required": ["mcp_slug"],
                    "additionalProperties": False,
                },
                outputSchema=_MUTATION_SCHEMA,
            ),
            types.Tool(
                name="remove_mcp",
                description=(
                    "Disable an MCP from the caller's toolbox. Persists. Does not "
                    "delete anything upstream — just hides it from this user.\n\n"
                    "When to use: caller asks to drop an MCP, or you detect the "
                    "user no longer needs it.\n\n"
                    "Returns: {status: 'removed', mcp} or {error: '...'} if the "
                    "slug is not currently enabled.\n\n"
                    "Credit cost: free."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mcp_slug": {
                            "type": "string",
                            "description": "Slug currently in the toolbox (see `list_mcps`).",
                        }
                    },
                    "required": ["mcp_slug"],
                    "additionalProperties": False,
                },
                outputSchema=_MUTATION_SCHEMA,
            ),
            types.Tool(
                name="search_tools",
                description=(
                    "Keyword search across tool names and descriptions of every "
                    "*enabled* MCP in the toolbox.\n\n"
                    "When to use: you want to find a capability but don't know "
                    "which MCP provides it. Cheaper than enumerating each MCP "
                    "with `list_mcp_tools`. Failed upstream discoveries are "
                    "silently skipped.\n\n"
                    "Returns: JSON array of {mcp, mcp_name, tool, description, "
                    "inputSchema}. Empty array means no matches — try broader "
                    "keywords or `browse_mcps` to enable more MCPs.\n\n"
                    "Credit cost: free."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Case-insensitive substring matched against tool name and description.",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                outputSchema={
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "mcp": {"type": "string"},
                                    "mcp_name": {"type": "string"},
                                    "tool": {"type": "string"},
                                    "description": {"type": "string"},
                                    "inputSchema": {"type": "object"},
                                },
                            },
                        },
                        "error": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="list_mcp_tools",
                description=(
                    "List every tool exposed by a single enabled MCP, with its "
                    "JSON Schema.\n\n"
                    "When to use: you already know which MCP you need and want "
                    "its full tool surface (e.g. before `invoke_mcp_tool`). For "
                    "cross-MCP discovery, prefer `search_tools`.\n\n"
                    "Returns: JSON array of {name, description, inputSchema} or "
                    "{error, reason} if upstream discovery fails.\n\n"
                    "Credit cost: free. "
                    "Note: replaces the legacy tool name `list_tools`, which also "
                    "still works for one release."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mcp_slug": {
                            "type": "string",
                            "description": "Slug of an enabled MCP (see `list_mcps`).",
                        }
                    },
                    "required": ["mcp_slug"],
                    "additionalProperties": False,
                },
                outputSchema={
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": _UPSTREAM_TOOL_SCHEMA},
                        "error": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="invoke_mcp_tool",
                description=(
                    "Proxy a tool call to an upstream MCP. This is the only tool "
                    "that actually performs work on behalf of the user.\n\n"
                    "**CREDIT-GATED**: each call deducts `credit_cost_per_call` "
                    "credits from the user (see `browse_mcps` / `list_mcps`). If "
                    "the balance is insufficient the call returns "
                    "{\"error\": \"Insufficient credits...\"} without hitting the "
                    "upstream. Direct the user to /portal/credits to top up.\n\n"
                    "When to use: after you have identified the right MCP and tool "
                    "via `search_tools` or `list_mcp_tools`. Call `list_mcp_tools` "
                    "first if you are unsure of the exact `tool_name` or of the "
                    "shape that `arguments` should take.\n\n"
                    "Example:\n"
                    "  invoke_mcp_tool(\n"
                    "    mcp_slug=\"mcp-zotero-qdrant\",\n"
                    "    tool_name=\"search\",\n"
                    "    arguments={\"query\": \"HIV prevention Mozambique\", \"limit\": 5}\n"
                    "  )\n\n"
                    "Returns: the upstream tool's JSON-encoded result, or "
                    "{\"error\": \"...\"} on upstream failure / timeout. "
                    "Note: replaces the legacy tool name `call_tool`, which also "
                    "still works for one release."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mcp_slug": {
                            "type": "string",
                            "description": "Slug of an enabled MCP (must appear in `list_mcps`).",
                        },
                        "tool_name": {
                            "type": "string",
                            "description": "Exact tool name as returned by `list_mcp_tools` or `search_tools`.",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments conforming to the upstream tool's inputSchema. Pass {} if the tool takes no arguments.",
                        },
                    },
                    "required": ["mcp_slug", "tool_name", "arguments"],
                    "additionalProperties": False,
                },
                outputSchema={"type": "object"},
            ),
        ] + await _promoted_ui_tools()

    async def _promoted_ui_tools() -> list[types.Tool]:
        """Discover UI-bearing upstream tools and expose them as first-class
        gateway tools named `{slug}__{tool_name}`. Preserves `_meta` so
        MCP Apps hosts (Claude.ai, ChatGPT) can render their UI resource."""
        promoted: list[types.Tool] = []
        tool_lists = await asyncio.gather(
            *[_get_tools(mcp["slug"]) for mcp in enabled_mcps],
            return_exceptions=True,
        )
        for mcp, tools in zip(enabled_mcps, tool_lists):
            slug = mcp["slug"]
            if isinstance(tools, BaseException):
                print(
                    f"GATEWAY: UI-tool discovery failed for {slug}, skipping: {tools}",
                    file=sys.stderr,
                )
                continue
            for t in tools:
                if not _tool_has_ui(t):
                    continue
                promoted_name = f"{slug}__{t['name']}"
                _ui_tools[promoted_name] = {
                    "slug": slug,
                    "upstream_name": t["name"],
                    "descriptor": t,
                }
                promoted.append(
                    types.Tool(
                        name=promoted_name,
                        description=t.get("description", ""),
                        inputSchema=t.get("inputSchema") or {"type": "object", "properties": {}},
                        _meta=t.get("_meta") or None,
                    )
                )
        return promoted

    async def _get_tools(slug: str) -> list[dict]:
        """Return tools for slug, raising RuntimeError if discovery fails.

        Uses the module-level TTL cache keyed by (slug, user_id) so descriptors
        survive across gateway connections. Failures aren't cached.
        """
        cache_key = (slug, user_id)
        cached = _tool_cache.get(cache_key)
        if cached is not None:
            return cached
        mcp = mcp_by_slug.get(slug)
        if not mcp:
            return []
        tools = await fetch_tool_list(
            mcp["upstream_url"],
            mcp.get("upstream_api_key", ""),
            user_id=user_id,
            client_id=client_id,
            extra_headers=_extra_headers_for(mcp, user_id),
        )
        _tool_cache.set(cache_key, tools)
        return tools

    @server.call_tool()
    async def call_tool_handler(name: str, arguments: dict):
        # Promoted UI tool dispatch: `{slug}__{tool_name}` → forward upstream
        # preserving structuredContent and _meta so MCP Apps hosts can render.
        if name in _ui_tools or "__" in name:
            info = _ui_tools.get(name)
            if info is None and "__" in name:
                slug, _, upstream_name = name.partition("__")
                if slug in mcp_by_slug:
                    info = {"slug": slug, "upstream_name": upstream_name}
            if info is not None:
                slug = info["slug"]
                upstream_name = info["upstream_name"]
                mcp = mcp_by_slug.get(slug)
                if mcp is None:
                    return types.CallToolResult(
                        content=[types.TextContent(type="text", text=json.dumps({"error": f"MCP '{slug}' not enabled"}))],
                        isError=True,
                    )
                credit_cost = _get_credit_cost(slug)
                if credit_cost > 0:
                    new_balance = _deduct_credits(user_id, credit_cost)
                    if new_balance < 0:
                        return types.CallToolResult(
                            content=[types.TextContent(type="text", text=json.dumps({"error": "Insufficient credits. Visit your portal to buy more credits."}))],
                            isError=True,
                        )
                t0 = time.monotonic()
                try:
                    print(
                        f"GATEWAY: call_upstream_tool_structured {slug}/{upstream_name} "
                        f"user_id={user_id!r} client_id={client_id!r} url={mcp['upstream_url']}",
                        file=sys.stderr,
                    )
                    raw = await call_upstream_tool_structured(
                        mcp["upstream_url"], upstream_name, arguments,
                        api_key=mcp.get("upstream_api_key", ""),
                        user_id=user_id,
                        client_id=client_id,
                        extra_headers=_extra_headers_for(mcp, user_id),
                    )
                except RuntimeError as exc:
                    print(f"GATEWAY: upstream auth/config error {slug}/{upstream_name}: {exc}", file=sys.stderr)
                    try:
                        import sentry_sdk; sentry_sdk.capture_exception(exc)
                    except Exception:
                        pass
                    return types.CallToolResult(
                        content=[types.TextContent(type="text", text=json.dumps({"error": str(exc)}))],
                        isError=True,
                    )
                except Exception as exc:
                    flat = _flatten_exception_message(exc)
                    is_timeout = isinstance(exc, TimeoutError) or "timeout" in flat.lower()
                    msg = (
                        f"Upstream MCP '{slug}' timed out after retries."
                        if is_timeout else flat
                    )
                    print(f"GATEWAY: upstream error {slug}/{upstream_name}: {flat}", file=sys.stderr)
                    try:
                        import sentry_sdk; sentry_sdk.capture_exception(exc)
                    except Exception:
                        pass
                    return types.CallToolResult(
                        content=[types.TextContent(type="text", text=json.dumps({"error": msg}))],
                        isError=True,
                    )
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                # Build blocks from serialised dicts
                blocks: list = []
                for b in raw.get("content") or []:
                    if isinstance(b, dict):
                        btype = b.get("type", "text")
                        if btype == "text":
                            blocks.append(types.TextContent(type="text", text=b.get("text", "")))
                        else:
                            # Fall back to JSON dump of unknown block types
                            blocks.append(types.TextContent(type="text", text=json.dumps(b)))
                if not blocks:
                    blocks = [types.TextContent(type="text", text="")]
                _log_tool_call_async(
                    user_id, client_id, slug, upstream_name,
                    credits_used=credit_cost,
                    duration_ms=elapsed_ms,
                    response_bytes=len(json.dumps(raw).encode("utf-8")),
                )
                return types.CallToolResult(
                    content=blocks,
                    structuredContent=raw.get("structuredContent"),
                    _meta=raw.get("_meta"),
                    isError=bool(raw.get("isError", False)),
                )

        # Deprecated-name compatibility shim (remove after one release cycle).
        _legacy_aliases = {"list_tools": "list_mcp_tools", "call_tool": "invoke_mcp_tool"}
        if name in _legacy_aliases:
            new_name = _legacy_aliases[name]
            print(
                f"GATEWAY: deprecated tool name '{name}' used by client={client_id}, "
                f"routing to '{new_name}' — update your client to use the new name.",
                file=sys.stderr,
            )
            name = new_name

        # Build a structured dict per tool. The MCP SDK requires structuredContent
        # whenever a tool declares outputSchema; array-returning tools are wrapped
        # in {"items": [...]} since StructuredContent is dict-typed.
        structured: dict = {}

        if name == "list_mcps":
            structured = {"items": [
                {
                    "slug": m["slug"],
                    "name": m["name"],
                    "description": m["description"],
                    "category": m["category"],
                    "credit_cost_per_call": float(m.get("credit_cost_per_call") or 0),
                }
                for m in enabled_mcps
            ]}

        elif name == "browse_mcps":
            all_mcps = _get_all_published_mcps(_get_user_tier(user_id))
            enabled_slugs = set(mcp_by_slug.keys())
            structured = {"items": [
                {
                    "slug": m["slug"],
                    "name": m["name"],
                    "description": m["description"],
                    "category": m["category"],
                    "credit_cost_per_call": float(m.get("credit_cost_per_call") or 0),
                    "enabled": m["slug"] in enabled_slugs,
                }
                for m in all_mcps
            ]}

        elif name == "add_mcp":
            slug = arguments.get("mcp_slug", "")
            all_mcps = {m["slug"]: m for m in _get_all_published_mcps(_get_user_tier(user_id))}
            if slug not in all_mcps:
                structured = {"error": f"MCP '{slug}' not found or not published"}
            elif slug in mcp_by_slug:
                structured = {"status": "already_enabled", "mcp": slug}
            else:
                new_slugs = list(mcp_by_slug.keys()) + [slug]
                _update_user_mcps(user_id, new_slugs)
                mcp_by_slug[slug] = all_mcps[slug]
                enabled_mcps.append(all_mcps[slug])
                _invalidate_user_tool_cache(user_id)
                structured = {"status": "added", "mcp": slug, "name": all_mcps[slug]["name"]}

        elif name == "remove_mcp":
            slug = arguments.get("mcp_slug", "")
            if slug not in mcp_by_slug:
                structured = {"error": f"MCP '{slug}' is not in your toolbox"}
            else:
                new_slugs = [s for s in mcp_by_slug.keys() if s != slug]
                _update_user_mcps(user_id, new_slugs)
                del mcp_by_slug[slug]
                enabled_mcps[:] = [m for m in enabled_mcps if m["slug"] != slug]
                _invalidate_user_tool_cache(user_id)
                structured = {"status": "removed", "mcp": slug}

        elif name == "search_tools":
            q = (arguments.get("query") or "").lower()
            # Fan out discovery in parallel; one upstream failing must not block the rest.
            tool_lists = await asyncio.gather(
                *[_get_tools(mcp["slug"]) for mcp in enabled_mcps],
                return_exceptions=True,
            )
            results = []
            for mcp, tools in zip(enabled_mcps, tool_lists):
                if isinstance(tools, BaseException):
                    print(
                        f"GATEWAY: tool discovery failed for {mcp['slug']}, skipping in search: {tools}",
                        file=sys.stderr,
                    )
                    continue
                for t in tools:
                    if q in t["name"].lower() or q in t.get("description", "").lower():
                        results.append({
                            "mcp": mcp["slug"],
                            "mcp_name": mcp["name"],
                            "tool": t["name"],
                            "description": t.get("description", ""),
                            "inputSchema": t.get("inputSchema", {}),
                        })
            structured = {"items": results}

        elif name == "list_mcp_tools":
            slug = arguments.get("mcp_slug", "")
            if slug not in mcp_by_slug:
                structured = {"error": f"MCP '{slug}' not found"}
            else:
                try:
                    structured = {"items": await _get_tools(slug)}
                except Exception as exc:
                    reason = _flatten_exception_message(exc)
                    print(
                        f"GATEWAY: tool discovery failed for {slug}: {reason}",
                        file=sys.stderr,
                    )
                    try:
                        import sentry_sdk; sentry_sdk.capture_exception(exc)
                    except Exception:
                        pass
                    structured = {"error": "tool_discovery_failed", "reason": reason}

        elif name == "invoke_mcp_tool":
            slug = arguments.get("mcp_slug", "")
            tool_name = arguments.get("tool_name", "")
            tool_args = arguments.get("arguments", {})
            if slug not in mcp_by_slug:
                structured = {"error": f"MCP '{slug}' not found"}
            else:
                mcp = mcp_by_slug[slug]
                credit_cost = _get_credit_cost(slug)
                # Atomic credit gate — deduct before calling upstream (refund not implemented)
                if credit_cost > 0:
                    new_balance = _deduct_credits(user_id, credit_cost)
                    if new_balance < 0:
                        structured = {"error": "Insufficient credits. Visit your portal to buy more credits."}
                        text = json.dumps(structured)
                        return [types.TextContent(type="text", text=text)], structured
                t0 = time.monotonic()
                upstream_text: str | None = None
                try:
                    print(
                        f"GATEWAY: call_upstream_tool {slug}/{tool_name} "
                        f"user_id={user_id!r} client_id={client_id!r} url={mcp['upstream_url']}",
                        file=sys.stderr,
                    )
                    upstream_text = await call_upstream_tool(
                        mcp["upstream_url"], tool_name, tool_args,
                        mcp.get("upstream_api_key", ""),
                        user_id=user_id,
                        client_id=client_id,
                        extra_headers=_extra_headers_for(mcp, user_id),
                    )
                except RuntimeError as exc:
                    print(f"GATEWAY: upstream auth/config error {slug}/{tool_name}: {exc}", file=sys.stderr)
                    try:
                        import sentry_sdk
                        sentry_sdk.capture_exception(exc)
                    except Exception:
                        pass
                    structured = {"error": str(exc)}
                except (TimeoutError, Exception) as exc:
                    flat = _flatten_exception_message(exc)
                    is_timeout = isinstance(exc, TimeoutError) or "timeout" in flat.lower()
                    if is_timeout:
                        print(f"GATEWAY: upstream timeout {slug}/{tool_name} after retries: {flat}", file=sys.stderr)
                        error_msg = (
                            f"Upstream MCP '{slug}' timed out after retries. "
                            f"The server may be overloaded or unreachable. "
                            f"Try again later or increase MCP_CALL_TIMEOUT (current: {TOOL_CALL_TIMEOUT}s)."
                        )
                    else:
                        print(f"GATEWAY: upstream error {slug}/{tool_name}: {flat}", file=sys.stderr)
                        error_msg = flat
                    try:
                        import sentry_sdk
                        sentry_sdk.capture_exception(exc)
                    except Exception:
                        pass
                    structured = {"error": error_msg}
                else:
                    # Parse upstream JSON; wrap non-dict results so structured stays object-typed.
                    try:
                        parsed = json.loads(upstream_text) if upstream_text else None
                    except (TypeError, ValueError):
                        parsed = None
                    if isinstance(parsed, dict):
                        structured = parsed
                    else:
                        structured = {"result": parsed if parsed is not None else upstream_text}
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                text_for_log = upstream_text if upstream_text is not None else json.dumps(structured)
                resp_bytes = len(text_for_log.encode("utf-8")) if text_for_log else 0
                _log_tool_call_async(
                    user_id, client_id, slug, tool_name,
                    credits_used=credit_cost,
                    duration_ms=elapsed_ms, response_bytes=resp_bytes,
                )
                # Prefer raw upstream text for the human-readable block when available.
                text = upstream_text if upstream_text is not None else json.dumps(structured)
                return [types.TextContent(type="text", text=text)], structured

        else:
            structured = {"error": f"Unknown tool: {name}"}

        text = json.dumps(structured)
        return [types.TextContent(type="text", text=text)], structured

    @server.list_resources()
    async def list_resources_handler() -> list[types.Resource]:
        """Aggregate resources from upstream MCPs that expose UI-bearing tools.
        Populates `_resource_origin` so `read_resource_handler` can proxy by URI."""
        # Ensure _ui_tools is populated (Claude.ai may call list_resources before list_tools).
        if not _ui_tools:
            try:
                await _promoted_ui_tools()
            except Exception as exc:
                print(f"GATEWAY: UI-tool prefetch failed: {exc}", file=sys.stderr)

        origin_slugs = {info["slug"] for info in _ui_tools.values()}
        resources: list[types.Resource] = []
        for mcp in enabled_mcps:
            slug = mcp["slug"]
            if slug not in origin_slugs:
                continue
            try:
                items = await list_upstream_resources(
                    mcp["upstream_url"],
                    api_key=mcp.get("upstream_api_key", ""),
                    user_id=user_id,
                    client_id=client_id,
                    extra_headers=_extra_headers_for(mcp, user_id),
                )
            except Exception as exc:
                print(f"GATEWAY: list_resources failed for {slug}: {exc}", file=sys.stderr)
                continue
            for it in items:
                uri = it.get("uri")
                if not uri:
                    continue
                _resource_origin[uri] = slug
                resources.append(
                    types.Resource(
                        uri=uri,
                        name=it.get("name") or uri,
                        description=it.get("description"),
                        mimeType=it.get("mimeType"),
                        title=it.get("title"),
                        _meta=it.get("_meta") or None,
                    )
                )
        return resources

    @server.read_resource()
    async def read_resource_handler(uri):
        from mcp.server.lowlevel.helper_types import ReadResourceContents
        uri_str = str(uri)
        slug = _resource_origin.get(uri_str)
        if slug is None:
            # Try to resolve via list first
            try:
                await list_resources_handler()  # populates _resource_origin
            except Exception:
                pass
            slug = _resource_origin.get(uri_str)
        if slug is None:
            raise ValueError(f"Unknown resource: {uri_str}")
        mcp = mcp_by_slug.get(slug)
        if mcp is None:
            raise ValueError(f"MCP '{slug}' not enabled")
        raw = await read_upstream_resource(
            mcp["upstream_url"], uri_str,
            api_key=mcp.get("upstream_api_key", ""),
            user_id=user_id,
            client_id=client_id,
            extra_headers=_extra_headers_for(mcp, user_id),
        )
        out: list[ReadResourceContents] = []
        for c in raw.get("contents") or []:
            if not isinstance(c, dict):
                continue
            if "text" in c:
                out.append(ReadResourceContents(
                    content=c.get("text", ""),
                    mime_type=c.get("mimeType"),
                    meta=c.get("_meta"),
                ))
            elif "blob" in c:
                import base64
                try:
                    data = base64.b64decode(c["blob"])
                except Exception:
                    data = b""
                out.append(ReadResourceContents(
                    content=data,
                    mime_type=c.get("mimeType"),
                    meta=c.get("_meta"),
                ))
        return out

    return server


def _get_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # Fallback: ?token= or ?access_token= query param (for clients that can't set headers).
    qp = request.query_params
    return qp.get("token") or qp.get("access_token") or None


def _unauth_response(request: Request, detail: str = "Bearer token required") -> JSONResponse:
    """Return a 401 with OAuth discovery headers before the SSE transport starts.

    Advertises the path-specific protected-resource metadata URL via
    RFC 9728's `resource_metadata` WWW-Authenticate parameter so strict
    clients (Claude.ai) request the path-scoped PRM and receive a `resource`
    indicator matching the URL they called.
    """
    issuer = get_settings().OAUTH_ISSUER_URL.rstrip("/")
    request_path = request.url.path.lstrip("/")
    resource_metadata_url = (
        f"{issuer}/.well-known/oauth-protected-resource/{request_path}"
        if request_path
        else f"{issuer}/.well-known/oauth-protected-resource"
    )
    return JSONResponse(
        content={"error": "unauthorized", "error_description": detail},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="{issuer}",'
                f' error="invalid_token",'
                f' error_description="{detail}",'
                f' resource_metadata="{resource_metadata_url}"'
            ),
            "Link": f'<{issuer}/.well-known/oauth-authorization-server>; rel="oauth-authorization-server"',
            "Cache-Control": "no-store, no-cache",
        },
    )



async def _gateway_asgi(scope, receive, send):
    """Raw ASGI handler for gateway endpoints.

    Bypasses FastAPI's response pipeline entirely so the MCP transport
    can own the full ASGI response lifecycle without double-send issues.
    """
    request = Request(scope, receive, send)
    path = request.url.path
    # Extract user_id from /gateway/{user_id} or /gateway/{user_id}/mcp
    parts = path.strip("/").split("/")
    url_user_id = parts[1] if len(parts) >= 2 else ""

    token = _get_bearer(request)
    if not token:
        print(f"GATEWAY: no bearer token on {request.method} {path}", file=sys.stderr)
        response = _unauth_response(request)
        await response(scope, receive, send)
        return

    # Agent-token path — long-lived bearer keys minted from /portal/setup.
    if token.startswith("dsmoz_"):
        agent_row = AgentTokenProvider().lookup(token)
        if not agent_row:
            print(f"GATEWAY: invalid/revoked agent token on {request.method} {path}", file=sys.stderr)
            response = _unauth_response(request)
            await response(scope, receive, send)
            return
        token_user_id = agent_row["user_id"]
        try:
            AgentTokenProvider().touch_last_used(agent_row["id"])
        except Exception:
            pass
        client_id = _ensure_agent_client(token_user_id)
        at = None  # no OAuth access-token context
    else:
        provider = SupabaseOAuthProvider()
        at = provider.load_access_token(token)
        if at is None or at.is_revoked:
            print(f"GATEWAY: invalid/revoked token on {request.method} {path}", file=sys.stderr)
            response = _unauth_response(request)
            await response(scope, receive, send)
            return

        from src.crypto import now_unix
        if at.expires_at and at.expires_at < now_unix():
            print(f"GATEWAY: expired token for client {at.client_id} on {request.method} {path}", file=sys.stderr)
            response = _unauth_response(request)
            await response(scope, receive, send)
            return

        token_user_id = _resolve_user_id_for_token(at)
        if not token_user_id:
            print(
                f"GATEWAY: token for client {at.client_id} has no user binding — "
                f"unclaimed client cannot access gateway",
                file=sys.stderr,
            )
            response = _unauth_response(request, "Token is not bound to a user")
            await response(scope, receive, send)
            return
        client_id = at.client_id

    if url_user_id == "me":
        # /gateway/me — resolve user from token rather than URL
        url_user_id = token_user_id
    elif token_user_id != url_user_id:
        print(
            f"GATEWAY: user mismatch — token user_id={token_user_id!r} "
            f"vs URL user_id={url_user_id!r} on {request.method} {path}",
            file=sys.stderr,
        )
        response = _unauth_response(request, "Token does not match gateway URL")
        await response(scope, receive, send)
        return

    print(
        f"GATEWAY: auth OK for user={token_user_id} client={client_id}, "
        f"{request.method} {path}",
        file=sys.stderr,
    )
    try:
        enabled_mcps = _load_enabled_mcps(token_user_id)
    except Exception as exc:
        print(f"GATEWAY: DB error loading MCPs for {token_user_id}: {exc}", file=sys.stderr)
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        error = JSONResponse(
            content={"error": "internal_error", "error_description": "Failed to load user configuration"},
            status_code=500,
        )
        await error(scope, receive, send)
        return
    mcp_server = _build_mcp_server(token_user_id, client_id, enabled_mcps)
    transport = StreamableHTTPServerTransport(mcp_session_id=None)

    response_started = False
    response_completed = False

    async def guarded_send(message):
        nonlocal response_started, response_completed
        if message.get("type") == "http.response.start":
            response_started = True
        elif message.get("type") == "http.response.body" and not message.get("more_body", False):
            response_completed = True
        await send(message)

    async def run_stateless_server(*, task_status=anyio.TASK_STATUS_IGNORED):
        async with transport.connect() as (read_stream, write_stream):
            task_status.started()
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
                stateless=True,
            )

    try:
        async with anyio.create_task_group() as tg:
            await tg.start(run_stateless_server)
            print(f"GATEWAY: MCP server ready, handling request", file=sys.stderr)
            await transport.handle_request(scope, receive, guarded_send)
            print(f"GATEWAY: handle_request completed", file=sys.stderr)
            tg.cancel_scope.cancel()
    except BaseException as exc:
        is_cancelled = isinstance(exc, (anyio.get_cancelled_exc_class(),))
        if not is_cancelled:
            print(f"GATEWAY: exception: {type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(exc)
            except Exception:
                pass
        if not response_started and not is_cancelled:
            error = JSONResponse(
                content={"error": "internal_error", "error_description": str(exc)},
                status_code=500,
            )
            await error(scope, receive, send)
            response_started = True
            response_completed = True
        if is_cancelled:
            raise
    finally:
        with anyio.move_on_after(2, shield=True):
            await transport.terminate()
        if response_started and not response_completed:
            try:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            except Exception:
                pass


class GatewayASGI:
    """Raw ASGI middleware that intercepts /gateway/ requests before FastAPI.

    This avoids FastAPI's response pipeline which causes ASGI double-send
    errors when the MCP transport has already sent the response directly.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/gateway/"):
            await _gateway_asgi(scope, receive, send)
        else:
            await self.app(scope, receive, send)

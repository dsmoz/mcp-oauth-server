"""
Gateway SSE endpoint — one MCP endpoint per client.

GET  /gateway/{client_id}                    — SSE stream
POST /gateway/{client_id}/messages           — MCP message channel

Uses a single shared SseServerTransport per path so session IDs
created on the GET are findable by the POST.
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import anyio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from mcp.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp import types

from src.config import get_settings
from src.db import get_db
from src.gateway.upstream import call_upstream_tool, fetch_tool_list
from src.oauth.provider import SupabaseOAuthProvider

router = APIRouter()


def evict_transport(client_id: str) -> None:
    """No-op — kept for call-site compatibility. Streamable HTTP is stateless."""
    pass


async def start_cleanup_loop() -> None:
    """No-op — kept for main.py compatibility. No persistent transports to clean up."""
    while True:
        await asyncio.sleep(3600)


def _load_enabled_mcps(client_id: str) -> list[dict]:
    """Return MCPs the client has added, filtered to only published ones."""
    db = get_db()
    client_row = db.table("oauth_clients").select("allowed_mcp_resources").eq("client_id", client_id).limit(1).execute()
    if not client_row.data:
        return []
    slugs = client_row.data[0].get("allowed_mcp_resources") or []
    if not slugs:
        return []
    return (
        db.table("mcp_catalogue")
          .select("*")
          .in_("slug", slugs)
          .eq("is_published", True)
          .execute()
          .data or []
    )


def _get_credit_cost(mcp_slug: str) -> float:
    """Return credit_cost_per_call for an MCP slug (0 if not set)."""
    try:
        row = get_db().table("mcp_catalogue").select("credit_cost_per_call").eq("slug", mcp_slug).limit(1).execute()
        return float((row.data or [{}])[0].get("credit_cost_per_call") or 0)
    except Exception:
        return 0.0


def _deduct_credits(client_id: str, amount: float) -> None:
    """Atomically subtract credits from client balance."""
    try:
        db = get_db()
        row = db.table("oauth_clients").select("credit_balance").eq("client_id", client_id).limit(1).execute()
        current = float((row.data or [{}])[0].get("credit_balance") or 0)
        new_balance = max(0.0, current - amount)
        db.table("oauth_clients").update({"credit_balance": new_balance}).eq("client_id", client_id).execute()
    except Exception:
        pass


def _log_tool_call(client_id: str, mcp_slug: str, tool_name: str, credits_used: float = 0.0) -> None:
    try:
        get_db().table("oauth_usage_logs").insert({
            "client_id": client_id,
            "endpoint": f"gateway/{mcp_slug}/{tool_name}",
            "credits_used": credits_used,
        }).execute()
    except Exception:
        pass


def _get_all_published_mcps() -> list[dict]:
    return get_db().table("mcp_catalogue").select("*").eq("is_published", True).execute().data or []


def _update_client_mcps(client_id: str, slugs: list[str]) -> None:
    get_db().table("oauth_clients").update(
        {"allowed_mcp_resources": slugs}
    ).eq("client_id", client_id).execute()


def _build_mcp_server(client_id: str, enabled_mcps: list[dict]) -> Server:
    mcp_by_slug = {m["slug"]: m for m in enabled_mcps}
    _tool_cache: dict[str, list[dict]] = {}

    server = Server("DS-MOZ Intelligence Gateway")

    @server.list_tools()
    async def list_tools_handler() -> list[types.Tool]:
        return [
            types.Tool(
                name="list_mcps",
                description="List all MCP servers currently in your toolbox.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="browse_mcps",
                description="Browse all available MCP servers you can add to your toolbox.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="add_mcp",
                description="Add an MCP server to your toolbox by slug. Use browse_mcps to discover available MCPs.",
                inputSchema={
                    "type": "object",
                    "properties": {"mcp_slug": {"type": "string"}},
                    "required": ["mcp_slug"],
                },
            ),
            types.Tool(
                name="remove_mcp",
                description="Remove an MCP server from your toolbox by slug.",
                inputSchema={
                    "type": "object",
                    "properties": {"mcp_slug": {"type": "string"}},
                    "required": ["mcp_slug"],
                },
            ),
            types.Tool(
                name="search_tools",
                description="Search for tools by keyword across all your enabled MCPs.",
                inputSchema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="list_tools",
                description="List all tools for a specific MCP (by slug).",
                inputSchema={
                    "type": "object",
                    "properties": {"mcp_slug": {"type": "string"}},
                    "required": ["mcp_slug"],
                },
            ),
            types.Tool(
                name="call_tool",
                description="Call a tool on an upstream MCP server.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mcp_slug": {"type": "string"},
                        "tool_name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["mcp_slug", "tool_name", "arguments"],
                },
            ),
        ]

    async def _get_tools(slug: str) -> list[dict]:
        if slug not in _tool_cache:
            mcp = mcp_by_slug.get(slug)
            _tool_cache[slug] = await fetch_tool_list(mcp["upstream_url"], mcp.get("upstream_api_key", "")) if mcp else []
        return _tool_cache[slug]

    @server.call_tool()
    async def call_tool_handler(name: str, arguments: dict) -> list[types.TextContent]:
        if name == "list_mcps":
            text = json.dumps([
                {"slug": m["slug"], "name": m["name"], "description": m["description"], "category": m["category"]}
                for m in enabled_mcps
            ])

        elif name == "browse_mcps":
            all_mcps = _get_all_published_mcps()
            enabled_slugs = set(mcp_by_slug.keys())
            text = json.dumps([
                {
                    "slug": m["slug"],
                    "name": m["name"],
                    "description": m["description"],
                    "category": m["category"],
                    "enabled": m["slug"] in enabled_slugs,
                }
                for m in all_mcps
            ])

        elif name == "add_mcp":
            slug = arguments.get("mcp_slug", "")
            all_mcps = {m["slug"]: m for m in _get_all_published_mcps()}
            if slug not in all_mcps:
                text = json.dumps({"error": f"MCP '{slug}' not found or not published"})
            elif slug in mcp_by_slug:
                text = json.dumps({"status": "already_enabled", "mcp": slug})
            else:
                new_slugs = list(mcp_by_slug.keys()) + [slug]
                _update_client_mcps(client_id, new_slugs)
                mcp_by_slug[slug] = all_mcps[slug]
                enabled_mcps.append(all_mcps[slug])
                text = json.dumps({"status": "added", "mcp": slug, "name": all_mcps[slug]["name"]})

        elif name == "remove_mcp":
            slug = arguments.get("mcp_slug", "")
            if slug not in mcp_by_slug:
                text = json.dumps({"error": f"MCP '{slug}' is not in your toolbox"})
            else:
                new_slugs = [s for s in mcp_by_slug.keys() if s != slug]
                _update_client_mcps(client_id, new_slugs)
                del mcp_by_slug[slug]
                enabled_mcps[:] = [m for m in enabled_mcps if m["slug"] != slug]
                text = json.dumps({"status": "removed", "mcp": slug})

        elif name == "search_tools":
            q = (arguments.get("query") or "").lower()
            results = []
            for mcp in enabled_mcps:
                for t in await _get_tools(mcp["slug"]):
                    if q in t["name"].lower() or q in t.get("description", "").lower():
                        results.append({"mcp": mcp["slug"], "mcp_name": mcp["name"],
                                        "tool": t["name"], "description": t.get("description", "")})
            text = json.dumps(results)

        elif name == "list_tools":
            slug = arguments.get("mcp_slug", "")
            text = json.dumps(await _get_tools(slug) if slug in mcp_by_slug
                              else {"error": f"MCP '{slug}' not found"})

        elif name == "call_tool":
            slug = arguments.get("mcp_slug", "")
            tool_name = arguments.get("tool_name", "")
            tool_args = arguments.get("arguments", {})
            if slug not in mcp_by_slug:
                text = json.dumps({"error": f"MCP '{slug}' not found"})
            else:
                mcp = mcp_by_slug[slug]
                credit_cost = _get_credit_cost(slug)
                # Credit gate — check balance before calling upstream
                if credit_cost > 0:
                    try:
                        bal_row = get_db().table("oauth_clients").select("credit_balance").eq("client_id", client_id).limit(1).execute()
                        balance = float((bal_row.data or [{}])[0].get("credit_balance") or 0)
                    except Exception:
                        balance = 0.0
                    if balance < credit_cost:
                        text = json.dumps({"error": "Insufficient credits. Visit your portal to buy more credits."})
                        return [types.TextContent(type="text", text=text)]
                try:
                    text = await call_upstream_tool(mcp["upstream_url"], tool_name, tool_args,
                                                    mcp.get("upstream_api_key", ""))
                    if credit_cost > 0:
                        _deduct_credits(client_id, credit_cost)
                except Exception as exc:
                    text = json.dumps({"error": str(exc)})
                _log_tool_call(client_id, slug, tool_name, credits_used=credit_cost)

        else:
            text = json.dumps({"error": f"Unknown tool: {name}"})

        return [types.TextContent(type="text", text=text)]

    return server


def _get_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth[7:]


def _unauth_response(request: Request) -> JSONResponse:
    """Return a 401 with OAuth discovery headers before the SSE transport starts."""
    issuer = get_settings().OAUTH_ISSUER_URL
    return JSONResponse(
        content={"error": "unauthorized", "error_description": "Bearer token required"},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="{issuer}",'
                f' error="invalid_token",'
                f' error_description="Bearer token required"'
            ),
            "Link": f'<{issuer}/.well-known/oauth-authorization-server>; rel="oauth-authorization-server"',
        },
    )


async def _run_streamable_http(client_id: str, request: Request):
    """Shared handler for Streamable HTTP transport (MCP spec 2025-03-26)."""
    token = _get_bearer(request)
    if not token:
        return _unauth_response(request)

    provider = SupabaseOAuthProvider()
    at = provider.load_access_token(token)
    if at is None:
        return _unauth_response(request)

    actual_client_id = at.client_id
    enabled_mcps = _load_enabled_mcps(actual_client_id)
    mcp_server = _build_mcp_server(actual_client_id, enabled_mcps)
    transport = StreamableHTTPServerTransport(mcp_session_id=None)

    async def run_stateless_server(*, task_status=anyio.TASK_STATUS_IGNORED):
        async with transport.connect() as (read_stream, write_stream):
            task_status.started()  # signals: ready, proceed with handle_request
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
                stateless=True,
            )

    from starlette.requests import ClientDisconnect
    try:
        async with anyio.create_task_group() as tg:
            await tg.start(run_stateless_server)  # waits until server is ready
            await transport.handle_request(request.scope, request.receive, request._send)
            tg.cancel_scope.cancel()
    except (ClientDisconnect, anyio.EndOfStream, Exception):
        pass  # client disconnected — terminate quietly
    finally:
        with anyio.move_on_after(2, shield=True):
            await transport.terminate()


# Primary endpoint — Streamable HTTP for all methods
# GET also uses Streamable HTTP (Claude Desktop probes with GET before POST)
@router.api_route("/gateway/{client_id}", methods=["GET", "POST", "DELETE"])
async def gateway_endpoint(client_id: str, request: Request):
    return await _run_streamable_http(client_id, request)


# Also handle /mcp suffix for clients that use it explicitly
@router.api_route("/gateway/{client_id}/mcp", methods=["GET", "POST", "DELETE"])
async def gateway_streamable_http_mcp(client_id: str, request: Request):
    return await _run_streamable_http(client_id, request)

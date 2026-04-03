"""
Gateway SSE endpoint — one MCP endpoint per client.

GET  /gateway/{client_id}          — SSE stream
POST /gateway/{client_id}/messages — MCP message channel

Validates Bearer token, loads the client's enabled MCPs from mcp_catalogue,
exposes 4 meta-tools via FastMCP:
  search_tools(query)                        → keyword search across tool names/descriptions
  list_mcps()                                → list client's enabled MCPs
  list_tools(mcp_slug)                       → list tools for one MCP
  call_tool(mcp_slug, tool_name, arguments)  → proxy call to upstream SSE, log usage
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp import types

from src.db import get_db
from src.gateway.upstream import call_upstream_tool, fetch_tool_list
from src.oauth.provider import SupabaseOAuthProvider

router = APIRouter()


def _validate_token(token: str) -> str:
    """Validate Bearer token. Returns actual client_id from DB."""
    provider = SupabaseOAuthProvider()
    at = provider.load_access_token(token)
    if at is None:
        raise HTTPException(status_code=401, detail="Invalid or expired access token")
    return at.client_id


def _load_enabled_mcps(client_id: str) -> list[dict]:
    """Return published mcp_catalogue rows for slugs the client has enabled."""
    db = get_db()
    client_row = db.table("oauth_clients").select("allowed_mcp_resources").eq("client_id", client_id).limit(1).execute()
    if not client_row.data:
        return []
    slugs = client_row.data[0].get("allowed_mcp_resources") or []
    if not slugs:
        return []
    catalogue = (
        db.table("mcp_catalogue")
          .select("*")
          .in_("slug", slugs)
          .eq("is_published", True)
          .execute()
          .data or []
    )
    return catalogue


def _log_tool_call(client_id: str, mcp_slug: str, tool_name: str) -> None:
    try:
        db = get_db()
        db.table("oauth_usage_logs").insert({
            "client_id": client_id,
            "endpoint": f"gateway/{mcp_slug}/{tool_name}",
        }).execute()
    except Exception:
        pass


def _build_mcp_server(client_id: str, enabled_mcps: list[dict]) -> Server:
    """Build a low-level MCP Server with 4 meta-tools."""
    mcp_by_slug = {m["slug"]: m for m in enabled_mcps}
    _tool_cache: dict[str, list[dict]] = {}

    server = Server("DS-MOZ Intelligence Gateway")

    @server.list_tools()
    async def list_tools_handler() -> list[types.Tool]:
        return [
            types.Tool(
                name="list_mcps",
                description="List all MCP servers the client has enabled.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="search_tools",
                description="Search for tools by keyword across all enabled MCPs.",
                inputSchema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search keyword"}},
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
            if not mcp:
                return []
            _tool_cache[slug] = await fetch_tool_list(mcp["upstream_url"], mcp.get("upstream_api_key", ""))
        return _tool_cache[slug]

    @server.call_tool()
    async def call_tool_handler(name: str, arguments: dict) -> list[types.TextContent]:
        if name == "list_mcps":
            result = json.dumps([
                {"slug": m["slug"], "name": m["name"], "description": m["description"], "category": m["category"]}
                for m in enabled_mcps
            ])

        elif name == "search_tools":
            query = (arguments.get("query") or "").lower()
            results = []
            for mcp in enabled_mcps:
                tools = await _get_tools(mcp["slug"])
                for t in tools:
                    if query in t["name"].lower() or query in t.get("description", "").lower():
                        results.append({
                            "mcp": mcp["slug"],
                            "mcp_name": mcp["name"],
                            "tool": t["name"],
                            "description": t.get("description", ""),
                        })
            result = json.dumps(results)

        elif name == "list_tools":
            slug = arguments.get("mcp_slug", "")
            if slug not in mcp_by_slug:
                result = json.dumps({"error": f"MCP '{slug}' not found or not enabled"})
            else:
                result = json.dumps(await _get_tools(slug))

        elif name == "call_tool":
            slug = arguments.get("mcp_slug", "")
            tool_name = arguments.get("tool_name", "")
            tool_args = arguments.get("arguments", {})
            if slug not in mcp_by_slug:
                result = json.dumps({"error": f"MCP '{slug}' not found or not enabled"})
            else:
                mcp = mcp_by_slug[slug]
                try:
                    result = await call_upstream_tool(
                        mcp["upstream_url"], tool_name, tool_args, mcp.get("upstream_api_key", "")
                    )
                except Exception as exc:
                    result = json.dumps({"error": str(exc)})
                _log_tool_call(client_id, slug, tool_name)
        else:
            result = json.dumps({"error": f"Unknown tool: {name}"})

        return [types.TextContent(type="text", text=result)]

    return server


def _get_bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    return auth[7:]


@router.get("/gateway/{client_id}")
async def gateway_sse(client_id: str, request: Request):
    """SSE stream — Claude Desktop connects here."""
    token = _get_bearer(request)
    actual_client_id = _validate_token(token)
    enabled_mcps = _load_enabled_mcps(actual_client_id)
    mcp_server = _build_mcp_server(actual_client_id, enabled_mcps)

    messages_path = f"/gateway/{client_id}/messages"
    transport = SseServerTransport(messages_path)

    async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp_server.run(streams[0], streams[1], mcp_server.create_initialization_options())


@router.post("/gateway/{client_id}/messages")
async def gateway_messages(client_id: str, request: Request):
    """MCP POST message channel."""
    token = _get_bearer(request)
    _validate_token(token)

    messages_path = f"/gateway/{client_id}/messages"
    transport = SseServerTransport(messages_path)
    return await transport.handle_post_message(request.scope, request.receive, request._send)

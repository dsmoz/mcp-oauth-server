"""
Gateway SSE endpoint — one MCP endpoint per client.

GET /gateway/{client_id}  — SSE stream

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
from fastapi.responses import StreamingResponse
from mcp.server.fastmcp import FastMCP

from src.db import get_db
from src.gateway.upstream import call_upstream_tool, fetch_tool_list
from src.oauth.provider import SupabaseOAuthProvider

router = APIRouter()


def _validate_token(token: str, client_id: str) -> None:
    """Raise HTTPException if token is invalid or doesn't belong to client_id."""
    provider = SupabaseOAuthProvider()
    at = provider.load_access_token(token)
    if at is None:
        raise HTTPException(status_code=401, detail="Invalid or expired access token")
    if at.client_id != client_id:
        raise HTTPException(status_code=403, detail="Token does not belong to this client")


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
        pass  # usage logging is best-effort


def _build_gateway_app(client_id: str, enabled_mcps: list[dict]) -> FastMCP:
    """Build a FastMCP instance with 4 meta-tools for this client's enabled MCPs."""
    mcp_by_slug = {m["slug"]: m for m in enabled_mcps}

    # Cache tool lists per slug within this session
    _tool_cache: dict[str, list[dict]] = {}

    async def _get_tools(slug: str) -> list[dict]:
        if slug not in _tool_cache:
            mcp = mcp_by_slug.get(slug)
            if not mcp:
                return []
            _tool_cache[slug] = await fetch_tool_list(mcp["upstream_url"], mcp.get("upstream_api_key", ""))
        return _tool_cache[slug]

    server = FastMCP(
        name="DS-MOZ Intelligence Gateway",
        instructions=(
            "You have access to the DS-MOZ Intelligence tools. "
            "Use search_tools() to discover available tools, list_mcps() to see enabled MCPs, "
            "list_tools(mcp_slug) to see tools for a specific MCP, "
            "and call_tool(mcp_slug, tool_name, arguments) to execute a tool."
        ),
    )

    @server.tool()
    async def list_mcps() -> str:
        """List all MCP servers the client has enabled."""
        return json.dumps([
            {"slug": m["slug"], "name": m["name"], "description": m["description"], "category": m["category"]}
            for m in enabled_mcps
        ])

    @server.tool()
    async def search_tools(query: str) -> str:
        """Search for tools by keyword across all enabled MCPs. Returns matching tools with their MCP slug."""
        query_lower = query.lower()
        results = []
        for mcp in enabled_mcps:
            tools = await _get_tools(mcp["slug"])
            for t in tools:
                if query_lower in t["name"].lower() or query_lower in t.get("description", "").lower():
                    results.append({
                        "mcp": mcp["slug"],
                        "mcp_name": mcp["name"],
                        "tool": t["name"],
                        "description": t.get("description", ""),
                    })
        return json.dumps(results)

    @server.tool()
    async def list_tools(mcp_slug: str) -> str:
        """List all tools available for a specific MCP (identified by its slug)."""
        if mcp_slug not in mcp_by_slug:
            return json.dumps({"error": f"MCP '{mcp_slug}' not found or not enabled"})
        tools = await _get_tools(mcp_slug)
        return json.dumps(tools)

    @server.tool()
    async def call_tool(mcp_slug: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool on an upstream MCP server. Returns the tool result as a string."""
        if mcp_slug not in mcp_by_slug:
            return json.dumps({"error": f"MCP '{mcp_slug}' not found or not enabled"})
        mcp = mcp_by_slug[mcp_slug]
        try:
            result = await call_upstream_tool(
                mcp["upstream_url"], tool_name, arguments, mcp.get("upstream_api_key", "")
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        _log_tool_call(client_id, mcp_slug, tool_name)
        return result

    return server


@router.get("/gateway/{client_id}")
async def gateway_sse(client_id: str, request: Request):
    """SSE gateway endpoint — validates Bearer token then streams FastMCP SSE."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth_header[7:]

    _validate_token(token, client_id)
    enabled_mcps = _load_enabled_mcps(client_id)
    gateway = _build_gateway_app(client_id, enabled_mcps)

    # Mount the FastMCP SSE app and forward the request
    sse_app = gateway.sse_app()
    return await sse_app(request.scope, request.receive, request._send)

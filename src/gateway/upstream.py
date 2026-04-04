"""
Upstream MCP client — connects to upstream MCP servers to fetch tool
lists and proxy tool calls.

Supports both transports:
- SSE transport: URLs ending in /sse
- Streamable HTTP transport: all other URLs (e.g. /mcp)
"""
from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


def _is_sse(url: str) -> bool:
    path = url.split("?")[0].rstrip("/")
    return path.endswith("/sse")


async def _list_tools_via_url(url: str, headers: dict) -> list[dict]:
    """Try to fetch tools from a single URL. Raises on failure."""
    if _is_sse(url):
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
    else:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()

    return [
        {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
        }
        for t in (result.tools or [])
    ]


async def fetch_tool_list(upstream_url: str, api_key: str = "") -> list[dict]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    base = upstream_url.rstrip("/").removesuffix("/sse").removesuffix("/mcp")

    # Build candidate URLs — try both with and without trailing slash to handle
    # servers that redirect /mcp → /mcp/, as well as SSE vs streamable-http variants
    normalised = upstream_url.rstrip("/")
    if normalised.endswith("/sse"):
        candidates = [normalised, f"{base}/mcp/", f"{base}/mcp"]
    elif normalised.endswith("/mcp"):
        candidates = [normalised, f"{base}/mcp/", f"{base}/sse"]
    else:
        candidates = [f"{base}/mcp/", f"{base}/mcp", f"{base}/sse"]

    last_exc: Exception | None = None
    for url in candidates:
        try:
            return await _list_tools_via_url(url, headers)
        except Exception as exc:
            last_exc = exc
            continue

    import logging
    logging.getLogger(__name__).warning("fetch_tool_list failed for %s: %s", upstream_url, last_exc)
    return []


async def call_upstream_tool(
    upstream_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    api_key: str = "",
) -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    if _is_sse(upstream_url):
        async with sse_client(upstream_url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
    else:
        async with streamablehttp_client(upstream_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)

    if result.content:
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                parts.append(str(block.data))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return json.dumps({"result": None})

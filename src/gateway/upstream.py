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


async def fetch_tool_list(upstream_url: str, api_key: str = "") -> list[dict]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        if _is_sse(upstream_url):
            async with sse_client(upstream_url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
        else:
            async with streamablehttp_client(upstream_url, headers=headers) as (read, write, _):
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
    except Exception:
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

"""
Upstream MCP client — connects to upstream SSE MCP servers to fetch tool
lists and proxy tool calls.
"""
from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client


async def fetch_tool_list(upstream_url: str, api_key: str = "") -> list[dict]:
    """
    Connect to upstream SSE MCP server and return its tool list.

    Returns a list of dicts with keys: name, description, inputSchema.
    Returns [] on any connection error (upstream may be down).
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with sse_client(upstream_url, headers=headers) as (read, write):
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
    """
    Connect to upstream SSE MCP server and call a tool.

    Returns the tool result as a JSON string.
    Raises RuntimeError on upstream error.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with sse_client(upstream_url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)

    # Flatten content blocks to a single string
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

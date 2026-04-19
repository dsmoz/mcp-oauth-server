"""
Upstream MCP client — connects to upstream MCP servers to fetch tool
lists and proxy tool calls.

Supports both transports:
- SSE transport: URLs ending in /sse
- Streamable HTTP transport: all other URLs (e.g. /mcp)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

# Timeouts for upstream MCP connections — configurable via env vars
TOOL_CALL_TIMEOUT = int(os.getenv("MCP_CALL_TIMEOUT", "120"))
TOOL_LIST_TIMEOUT = int(os.getenv("MCP_LIST_TIMEOUT", "15"))

# Retry configuration for transient failures
_RETRY_MAX_ATTEMPTS = 2  # 1 original + 1 retry
_RETRY_BACKOFF_SECONDS = 3


def _is_sse(url: str) -> bool:
    path = url.split("?")[0].rstrip("/")
    return path.endswith("/sse")


async def _list_tools_via_url(url: str, headers: dict) -> list[dict]:
    """Try to fetch tools from a single URL. Raises on failure."""
    with anyio.fail_after(TOOL_LIST_TIMEOUT):
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


async def fetch_tool_list(
    upstream_url: str,
    api_key: str = "",
    user_id: str = "",
    client_id: str = "",
) -> list[dict]:
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if user_id:
        headers["X-User-ID"] = user_id
    if client_id:
        headers["X-Client-ID"] = client_id
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

    raise RuntimeError(
        f"Tool discovery failed for {upstream_url}: {last_exc}"
    ) from last_exc


def _is_auth_error(exc: Exception) -> bool:
    """Check whether an exception indicates a 401 Unauthorized from upstream.

    Args:
        exc: The exception to inspect.

    Returns:
        True if the exception wraps an HTTP 401 response.
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        return True
    # The MCP SDK may wrap httpx errors — walk the cause chain
    cause = exc.__cause__ or exc.__context__
    while cause is not None:
        if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 401:
            return True
        cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
    # Also check the string representation as a last resort (some transports
    # surface status codes only in the message)
    msg = str(exc).lower()
    return "401" in msg and ("unauthorized" in msg or "authentication" in msg)


def _is_timeout_error(exc: Exception) -> bool:
    """Check whether an exception is a transient timeout.

    Args:
        exc: The exception to inspect.

    Returns:
        True if the exception represents a timeout that may succeed on retry.
    """
    if isinstance(exc, (TimeoutError, anyio.get_cancelled_exc_class())):
        return True
    if isinstance(exc, httpx.TimeoutException):
        return True
    cause = exc.__cause__ or exc.__context__
    while cause is not None:
        if isinstance(cause, (TimeoutError, httpx.TimeoutException)):
            return True
        cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
    return False


async def _do_upstream_call(
    upstream_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    headers: dict[str, str],
) -> str:
    """Execute a single upstream tool call attempt.

    Args:
        upstream_url: The upstream MCP server URL.
        tool_name: Name of the tool to call.
        arguments: Tool arguments dict.
        headers: HTTP headers including auth.

    Returns:
        The concatenated text result from the upstream tool.

    Raises:
        Exception: Any error from the upstream call (timeout, auth, network).
    """
    with anyio.fail_after(TOOL_CALL_TIMEOUT):
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


async def call_upstream_tool(
    upstream_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    api_key: str = "",
    user_id: str = "",
    client_id: str = "",
) -> str:
    """Call a tool on an upstream MCP server with retry and error handling.

    Retries once on transient timeouts with backoff. Raises a clear error
    for 401 auth failures so operators know to refresh the upstream token.

    Args:
        upstream_url: The upstream MCP server URL.
        tool_name: Name of the tool to call.
        arguments: Tool arguments dict.
        api_key: Bearer token for the upstream server.
        user_id: User (tenant) ID forwarded via X-User-ID — this is the
            namespace key upstream MCPs use to isolate per-tenant state.
        client_id: Client (device) ID forwarded via X-Client-ID — telemetry
            only; upstream MCPs should NOT use this as a tenancy key.

    Returns:
        The text result from the upstream tool.

    Raises:
        RuntimeError: On 401 auth failure with an actionable message.
        TimeoutError: When all retry attempts are exhausted.
        Exception: Other upstream errors.
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if user_id:
        headers["X-User-ID"] = user_id
    if client_id:
        headers["X-Client-ID"] = client_id

    sys.stderr.write(
        f"UPSTREAM: {tool_name} headers={list(headers.keys())} "
        f"X-User-ID={headers.get('X-User-ID', 'NOT SET')} "
        f"X-Client-ID={headers.get('X-Client-ID', 'NOT SET')} "
        f"timeout={TOOL_CALL_TIMEOUT}s\n"
    )

    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            return await _do_upstream_call(upstream_url, tool_name, arguments, headers)

        except Exception as exc:
            last_exc = exc

            # 401 — no point retrying, the token is bad
            if _is_auth_error(exc):
                msg = (
                    f"Upstream MCP server at {upstream_url} returned 401 Unauthorized. "
                    f"The upstream_api_key for this MCP is expired or misconfigured. "
                    f"Update the upstream_api_key in the mcp_catalogue table and redeploy."
                )
                logger.error(msg)
                raise RuntimeError(msg) from exc

            # Timeout — retry once with backoff
            if _is_timeout_error(exc) and attempt < _RETRY_MAX_ATTEMPTS:
                logger.warning(
                    "Upstream timeout on attempt %d/%d for %s/%s (timeout=%ds). "
                    "Retrying in %ds...",
                    attempt, _RETRY_MAX_ATTEMPTS, upstream_url, tool_name,
                    TOOL_CALL_TIMEOUT, _RETRY_BACKOFF_SECONDS,
                )
                await anyio.sleep(_RETRY_BACKOFF_SECONDS)
                continue

            # Non-retryable or final attempt — re-raise
            raise

    # Should not reach here, but satisfy type checker
    assert last_exc is not None
    raise last_exc

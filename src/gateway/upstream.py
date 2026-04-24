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
from mcp.shared.exceptions import McpError

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


def _extract_tool_meta(t) -> dict | None:
    """Return the tool descriptor's `_meta` dict (aliased as `meta` on the
    pydantic model) so MCP Apps UI pointers survive the proxy hop."""
    meta = getattr(t, "meta", None)
    if meta is None:
        meta = getattr(t, "_meta", None)
    if meta is None:
        return None
    if hasattr(meta, "model_dump"):
        return meta.model_dump(exclude_none=True)
    if isinstance(meta, dict):
        return meta
    return None


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

    out: list[dict] = []
    for t in (result.tools or []):
        entry = {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
        }
        meta = _extract_tool_meta(t)
        if meta:
            entry["_meta"] = meta
        out.append(entry)
    return out


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

    # Build candidate URLs. The registered URL is tried first VERBATIM —
    # never strip its trailing slash, because some upstreams (uvicorn behind
    # Railway) respond 307 to /mcp and the SDK's SSE GET listener loses its
    # Authorization header on the redirect, surfacing as a spurious 401.
    normalised = upstream_url.rstrip("/")
    if normalised.endswith("/sse"):
        alternates = [f"{base}/mcp/", f"{base}/mcp"]
    elif normalised.endswith("/mcp"):
        alternates = [f"{base}/mcp/", f"{base}/mcp", f"{base}/sse"]
    else:
        alternates = [f"{base}/mcp/", f"{base}/mcp", f"{base}/sse"]
    seen: set[str] = set()
    candidates: list[str] = []
    for u in [upstream_url, *alternates]:
        if u not in seen:
            seen.add(u)
            candidates.append(u)

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
def _walk_exceptions(exc: BaseException):
    """Yield exc and every nested cause/context/sub-exception (incl. groups)."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen or cur is None:
            continue
        seen.add(id(cur))
        yield cur
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        cause = getattr(cur, "__cause__", None)
        ctx = getattr(cur, "__context__", None)
        if cause is not None:
            stack.append(cause)
        if ctx is not None:
            stack.append(ctx)


def _is_session_terminated_error(exc: Exception) -> bool:
    """Detect upstream 'session terminated' / 404 errors from the streamable
    HTTP transport. These mean the upstream restarted or evicted the session;
    retrying at the same URL is pointless, but a fresh connection (e.g. via
    URL fallback) may succeed.
    """
    for e in _walk_exceptions(exc):
        if isinstance(e, McpError):
            msg = str(getattr(e, "args", [""])[0] if e.args else "").lower()
            if "session terminated" in msg or "session not found" in msg:
                return True
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
            return True
        msg = str(e).lower()
        if "session terminated" in msg or "session not found" in msg:
            return True
    return False


def _is_auth_error(exc: Exception) -> bool:
    """Check whether an exception indicates a 401 Unauthorized from upstream.

    Args:
        exc: The exception to inspect.

    Returns:
        True if the exception wraps an HTTP 401 response.
    """
    for e in _walk_exceptions(exc):
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 401:
            return True
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


def _candidate_urls(upstream_url: str) -> list[str]:
    """Build ordered URL candidates so a misregistered catalogue entry
    (e.g. `/sse` when the server only serves `/mcp`) does not hang calls.

    The registered URL is always tried first; alternates are tried only
    if it fails with a transient connect/timeout error.
    """
    normalised = upstream_url.rstrip("/")
    base = normalised.removesuffix("/sse").removesuffix("/mcp")
    if normalised.endswith("/sse"):
        alternates = [f"{base}/mcp/", f"{base}/mcp"]
    elif normalised.endswith("/mcp"):
        alternates = [f"{base}/mcp/", f"{base}/mcp", f"{base}/sse"]
    else:
        alternates = [f"{base}/mcp/", f"{base}/mcp", f"{base}/sse"]
    # Use the registered URL VERBATIM as the first candidate — preserving its
    # trailing slash avoids a 307 redirect chain that drops the Authorization
    # header on the SDK's SSE GET listener (manifests as a spurious 401).
    seen = set()
    ordered: list[str] = []
    for u in [upstream_url, *alternates]:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


async def _call_via_url(
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    headers: dict[str, str],
    timeout: int | None = None,
) -> str:
    """Execute a single upstream tool call attempt against one URL.

    Splits the timeout budget: session.initialize() gets a short window
    (TOOL_LIST_TIMEOUT) so a hung handshake fails fast and we can fall
    back to the next URL candidate. session.call_tool() gets the full
    per-call budget for the actual work.
    """
    call_timeout = timeout if timeout is not None else TOOL_CALL_TIMEOUT
    init_timeout = min(TOOL_LIST_TIMEOUT, call_timeout)
    if _is_sse(url):
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                with anyio.fail_after(init_timeout):
                    await session.initialize()
                with anyio.fail_after(call_timeout):
                    result = await session.call_tool(tool_name, arguments=arguments)
    else:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                with anyio.fail_after(init_timeout):
                    await session.initialize()
                with anyio.fail_after(call_timeout):
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


async def _do_upstream_call(
    upstream_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    headers: dict[str, str],
) -> str:
    """Execute an upstream tool call, falling back across URL variants
    on transient connect/timeout failures. Auth failures abort immediately.
    """
    candidates = _candidate_urls(upstream_url)
    last_exc: Exception | None = None
    for idx, url in enumerate(candidates):
        # Only the registered URL gets the full timeout budget. Alternates are
        # probes — capped at TOOL_LIST_TIMEOUT so a fully-wrong catalogue
        # entry cannot burn 3 × TOOL_CALL_TIMEOUT.
        timeout = TOOL_CALL_TIMEOUT if idx == 0 else TOOL_LIST_TIMEOUT
        try:
            return await _call_via_url(url, tool_name, arguments, headers, timeout=timeout)
        except Exception as exc:
            if _is_auth_error(exc):
                raise
            last_exc = exc
            transient = _is_timeout_error(exc) or _is_session_terminated_error(exc)
            if idx < len(candidates) - 1 and transient:
                logger.warning(
                    "Upstream call to %s timed out (budget=%ds); trying next candidate %s",
                    url, timeout, candidates[idx + 1],
                )
                continue
            raise
    assert last_exc is not None
    raise last_exc


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

            # Upstream session evicted / 404 — connection-level fault, not a
            # timeout. Surface a clear error rather than retrying or letting
            # an opaque ExceptionGroup bubble up.
            if _is_session_terminated_error(exc):
                msg = (
                    f"Upstream MCP server at {upstream_url} terminated the "
                    f"session (likely restarted or evicted state). "
                    f"Tool call '{tool_name}' could not be completed."
                )
                logger.warning(msg)
                raise RuntimeError(msg) from exc

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


# ---------------------------------------------------------------------------
# MCP Apps pass-through: structured tool calls + UI resource proxy
# ---------------------------------------------------------------------------


def _serialise_block(block: Any) -> dict:
    """Serialise an MCP content block to a JSON-safe dict preserving type."""
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True, mode="json")
    if isinstance(block, dict):
        return block
    # Last-resort fallback
    return {"type": "text", "text": str(block)}


def _headers(api_key: str, user_id: str, client_id: str) -> dict[str, str]:
    h: dict[str, str] = {}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    if user_id:
        h["X-User-ID"] = user_id
    if client_id:
        h["X-Client-ID"] = client_id
    return h


async def _call_structured_via_url(
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    headers: dict[str, str],
    timeout: int | None = None,
) -> dict:
    """Single-attempt structured call — preserves content + structuredContent + _meta."""
    with anyio.fail_after(timeout if timeout is not None else TOOL_CALL_TIMEOUT):
        if _is_sse(url):
            async with sse_client(url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)
        else:
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)

    content_blocks = [_serialise_block(b) for b in (result.content or [])]
    out: dict[str, Any] = {
        "content": content_blocks,
        "isError": bool(getattr(result, "isError", False)),
    }
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        out["structuredContent"] = sc
    meta = getattr(result, "meta", None) or getattr(result, "_meta", None)
    if meta is not None:
        if hasattr(meta, "model_dump"):
            meta = meta.model_dump(exclude_none=True, mode="json")
        out["_meta"] = meta
    return out


async def call_upstream_tool_structured(
    upstream_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    api_key: str = "",
    user_id: str = "",
    client_id: str = "",
) -> dict:
    """Call an upstream tool and return the full CallToolResult as a dict.

    Unlike `call_upstream_tool` (which flattens to text), this preserves
    `content` blocks, `structuredContent`, `_meta`, and `isError` — required
    so MCP Apps UI pointers (`_meta.ui.resourceUri`) and per-call
    structuredContent survive the gateway hop.
    """
    headers = _headers(api_key, user_id, client_id)
    candidates = _candidate_urls(upstream_url)
    last_exc: Exception | None = None
    for idx, url in enumerate(candidates):
        timeout = TOOL_CALL_TIMEOUT if idx == 0 else TOOL_LIST_TIMEOUT
        try:
            return await _call_structured_via_url(
                url, tool_name, arguments, headers, timeout=timeout
            )
        except Exception as exc:
            if _is_auth_error(exc):
                raise RuntimeError(
                    f"Upstream MCP server at {upstream_url} returned 401 Unauthorized. "
                    f"The upstream_api_key is expired or misconfigured."
                ) from exc
            last_exc = exc
            if idx < len(candidates) - 1 and _is_timeout_error(exc):
                logger.warning(
                    "Structured upstream call to %s timed out; trying next candidate",
                    url,
                )
                continue
            raise
    assert last_exc is not None
    raise last_exc


async def _list_resources_via_url(url: str, headers: dict) -> list[dict]:
    with anyio.fail_after(TOOL_LIST_TIMEOUT):
        if _is_sse(url):
            async with sse_client(url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_resources()
        else:
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_resources()

    out: list[dict] = []
    for r in (result.resources or []):
        entry = {
            "uri": str(r.uri),
            "name": r.name,
            "description": r.description or "",
        }
        if getattr(r, "mimeType", None):
            entry["mimeType"] = r.mimeType
        if getattr(r, "title", None):
            entry["title"] = r.title
        meta = getattr(r, "meta", None) or getattr(r, "_meta", None)
        if meta is not None:
            if hasattr(meta, "model_dump"):
                meta = meta.model_dump(exclude_none=True, mode="json")
            entry["_meta"] = meta
        out.append(entry)
    return out


async def list_upstream_resources(
    upstream_url: str,
    api_key: str = "",
    user_id: str = "",
    client_id: str = "",
) -> list[dict]:
    """List resources from an upstream MCP server. Returns [] on failure."""
    headers = _headers(api_key, user_id, client_id)
    last_exc: Exception | None = None
    for url in _candidate_urls(upstream_url):
        try:
            return await _list_resources_via_url(url, headers)
        except Exception as exc:
            last_exc = exc
            continue
    logger.warning("Resource listing failed for %s: %s", upstream_url, last_exc)
    return []


async def _read_resource_via_url(url: str, uri: str, headers: dict) -> dict:
    with anyio.fail_after(TOOL_CALL_TIMEOUT):
        if _is_sse(url):
            async with sse_client(url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.read_resource(uri)
        else:
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.read_resource(uri)

    contents: list[dict] = []
    for c in (result.contents or []):
        contents.append(_serialise_block(c))
    return {"contents": contents}


async def read_upstream_resource(
    upstream_url: str,
    uri: str,
    api_key: str = "",
    user_id: str = "",
    client_id: str = "",
) -> dict:
    """Read a resource (e.g. `ui://...`) from an upstream MCP server."""
    headers = _headers(api_key, user_id, client_id)
    last_exc: Exception | None = None
    for url in _candidate_urls(upstream_url):
        try:
            return await _read_resource_via_url(url, uri, headers)
        except Exception as exc:
            if _is_auth_error(exc):
                raise
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc

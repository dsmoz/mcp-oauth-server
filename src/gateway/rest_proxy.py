"""
REST proxy — forwards /api/plugin/* requests to mcp-scholar.

Authenticates the caller's OAuth access_token, resolves the client_id,
then proxies the request to mcp-scholar with admin credentials +
X-Client-ID header.
"""
from __future__ import annotations

import sys
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.crypto import hash_token, now_unix
from src.db import get_db
from src.oauth.provider import SupabaseOAuthProvider

router = APIRouter()

_MCP_SCHOLAR_SLUG = "mcp-scholar"


def _get_bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth[7:]


def _validate_token(token: str) -> Optional[str]:
    """Validate an OAuth access_token. Returns client_id if valid, None otherwise."""
    provider = SupabaseOAuthProvider()
    at = provider.load_access_token(token)
    if at is None or at.is_revoked:
        return None
    if at.expires_at and at.expires_at < now_unix():
        return None
    return at.client_id


def _get_scholar_config() -> Optional[dict]:
    """Look up mcp-scholar upstream_url and upstream_api_key from mcp_catalogue."""
    db = get_db()
    result = (
        db.table("mcp_catalogue")
        .select("upstream_url, upstream_api_key")
        .eq("slug", _MCP_SCHOLAR_SLUG)
        .eq("is_published", True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _log_rest_call(
    client_id: str, path: str,
    duration_ms: int | None = None, response_bytes: int | None = None,
) -> None:
    try:
        row: dict = {
            "client_id": client_id,
            "endpoint": f"rest_proxy/{path}",
            "credits_used": 0,
        }
        if duration_ms is not None:
            row["duration_ms"] = duration_ms
        if response_bytes is not None:
            row["response_bytes"] = response_bytes
        get_db().table("oauth_usage_logs").insert(row).execute()
    except Exception as exc:
        print(f"WARNING: rest_proxy usage log failed for {client_id}: {exc}", file=sys.stderr)


def _unauth() -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "error_description": "Valid access token required"},
        status_code=401,
    )


@router.api_route(
    "/api/plugin/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy_plugin_request(request: Request, path: str):
    """Proxy all /api/plugin/* requests to mcp-scholar."""
    token = _get_bearer(request)
    if not token:
        return _unauth()

    client_id = _validate_token(token)
    if not client_id:
        return _unauth()

    scholar = _get_scholar_config()
    if not scholar:
        return JSONResponse(
            {"error": "mcp-scholar service not available"},
            status_code=503,
        )

    # Build upstream URL: strip /mcp suffix from upstream_url, append /api/plugin/{path}
    base_url = scholar["upstream_url"].rstrip("/").removesuffix("/mcp").removesuffix("/sse")
    upstream_url = f"{base_url}/api/plugin/{path}"

    # Forward query string
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Build upstream headers
    upstream_headers = {
        "X-Client-ID": client_id,
        "Content-Type": request.headers.get("content-type", "application/json"),
    }
    if scholar.get("upstream_api_key"):
        upstream_headers["Authorization"] = f"Bearer {scholar['upstream_api_key']}"

    # Read request body
    body = await request.body()

    print(
        f"REST_PROXY: {request.method} /api/plugin/{path} → {upstream_url} client_id={client_id}",
        file=sys.stderr,
    )

    # Check if this is an SSE/streaming request (chat endpoints use Accept: text/event-stream)
    accept = request.headers.get("accept", "")
    is_sse = "text/event-stream" in accept

    t0 = time.monotonic()
    try:
        if is_sse:
            # Stream SSE responses back to the client
            client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
            upstream_req = client.build_request(
                method=request.method,
                url=upstream_url,
                headers=upstream_headers,
                content=body if body else None,
            )
            upstream_resp = await client.send(upstream_req, stream=True)
            total_bytes = 0

            async def stream_response():
                nonlocal total_bytes
                try:
                    async for chunk in upstream_resp.aiter_bytes():
                        total_bytes += len(chunk)
                        yield chunk
                finally:
                    await upstream_resp.aclose()
                    await client.aclose()
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    _log_rest_call(client_id, path, duration_ms=elapsed_ms, response_bytes=total_bytes)

            return StreamingResponse(
                stream_response(),
                status_code=upstream_resp.status_code,
                headers={
                    k: v for k, v in upstream_resp.headers.items()
                    if k.lower() in ("content-type", "cache-control", "x-accel-buffering")
                },
            )
        else:
            # Regular request — proxy and return
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                upstream_resp = await client.request(
                    method=request.method,
                    url=upstream_url,
                    headers=upstream_headers,
                    content=body if body else None,
                )
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                resp_bytes = len(upstream_resp.content)
                _log_rest_call(client_id, path, duration_ms=elapsed_ms, response_bytes=resp_bytes)
                return JSONResponse(
                    content=upstream_resp.json() if upstream_resp.headers.get("content-type", "").startswith("application/json") else {"raw": upstream_resp.text},
                    status_code=upstream_resp.status_code,
                )
    except httpx.TimeoutException:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_rest_call(client_id, path, duration_ms=elapsed_ms)
        return JSONResponse({"error": "Upstream timeout"}, status_code=504)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_rest_call(client_id, path, duration_ms=elapsed_ms)
        print(f"REST_PROXY: error proxying {path}: {exc}", file=sys.stderr)
        return JSONResponse({"error": "Proxy error", "detail": str(exc)}, status_code=502)

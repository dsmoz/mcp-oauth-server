"""
Gateway — one MCP endpoint per user (tenant) via Streamable HTTP transport.

GET/POST/DELETE  /gateway/{user_id}       — primary endpoint
GET/POST/DELETE  /gateway/{user_id}/mcp   — alias with /mcp suffix

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

from src.config import get_settings
from src.db import get_db
from src.gateway.upstream import call_upstream_tool, fetch_tool_list, TOOL_CALL_TIMEOUT
from src.oauth.provider import SupabaseOAuthProvider


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
    """Return MCPs the user has added, filtered to only published ones."""
    db = get_db()
    user_row = (
        db.table("users")
        .select("allowed_mcp_resources")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not user_row.data:
        return []
    slugs = user_row.data[0].get("allowed_mcp_resources") or []
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


def _get_all_published_mcps() -> list[dict]:
    return get_db().table("mcp_catalogue").select("*").eq("is_published", True).execute().data or []


def _update_user_mcps(user_id: str, slugs: list[str]) -> None:
    get_db().table("users").update(
        {"allowed_mcp_resources": slugs}
    ).eq("user_id", user_id).execute()


def _build_mcp_server(user_id: str, client_id: str, enabled_mcps: list[dict]) -> Server:
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
        """Return tools for slug, raising RuntimeError if discovery fails."""
        if slug not in _tool_cache:
            mcp = mcp_by_slug.get(slug)
            if not mcp:
                return []
            # May raise RuntimeError — don't cache failures so next call retries
            tools = await fetch_tool_list(
                mcp["upstream_url"],
                mcp.get("upstream_api_key", ""),
                user_id=user_id,
                client_id=client_id,
            )
            _tool_cache[slug] = tools
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
                _update_user_mcps(user_id, new_slugs)
                mcp_by_slug[slug] = all_mcps[slug]
                enabled_mcps.append(all_mcps[slug])
                text = json.dumps({"status": "added", "mcp": slug, "name": all_mcps[slug]["name"]})

        elif name == "remove_mcp":
            slug = arguments.get("mcp_slug", "")
            if slug not in mcp_by_slug:
                text = json.dumps({"error": f"MCP '{slug}' is not in your toolbox"})
            else:
                new_slugs = [s for s in mcp_by_slug.keys() if s != slug]
                _update_user_mcps(user_id, new_slugs)
                del mcp_by_slug[slug]
                enabled_mcps[:] = [m for m in enabled_mcps if m["slug"] != slug]
                text = json.dumps({"status": "removed", "mcp": slug})

        elif name == "search_tools":
            q = (arguments.get("query") or "").lower()
            results = []
            for mcp in enabled_mcps:
                try:
                    tools = await _get_tools(mcp["slug"])
                except Exception as exc:
                    print(
                        f"GATEWAY: tool discovery failed for {mcp['slug']}, skipping in search: {exc}",
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
            text = json.dumps(results)

        elif name == "list_tools":
            slug = arguments.get("mcp_slug", "")
            if slug not in mcp_by_slug:
                text = json.dumps({"error": f"MCP '{slug}' not found"})
            else:
                try:
                    text = json.dumps(await _get_tools(slug))
                except Exception as exc:
                    print(
                        f"GATEWAY: tool discovery failed for {slug}: {exc}",
                        file=sys.stderr,
                    )
                    text = json.dumps({"error": "tool_discovery_failed", "reason": str(exc)})

        elif name == "call_tool":
            slug = arguments.get("mcp_slug", "")
            tool_name = arguments.get("tool_name", "")
            tool_args = arguments.get("arguments", {})
            if slug not in mcp_by_slug:
                text = json.dumps({"error": f"MCP '{slug}' not found"})
            else:
                mcp = mcp_by_slug[slug]
                credit_cost = _get_credit_cost(slug)
                # Atomic credit gate — deduct before calling upstream (refund not implemented)
                if credit_cost > 0:
                    new_balance = _deduct_credits(user_id, credit_cost)
                    if new_balance < 0:
                        text = json.dumps({"error": "Insufficient credits. Visit your portal to buy more credits."})
                        return [types.TextContent(type="text", text=text)]
                t0 = time.monotonic()
                try:
                    print(
                        f"GATEWAY: call_upstream_tool {slug}/{tool_name} "
                        f"user_id={user_id!r} client_id={client_id!r} url={mcp['upstream_url']}",
                        file=sys.stderr,
                    )
                    text = await call_upstream_tool(
                        mcp["upstream_url"], tool_name, tool_args,
                        mcp.get("upstream_api_key", ""),
                        user_id=user_id,
                        client_id=client_id,
                    )
                except RuntimeError as exc:
                    # RuntimeError from upstream.py signals a known issue (e.g. 401 auth)
                    print(f"GATEWAY: upstream auth/config error {slug}/{tool_name}: {exc}", file=sys.stderr)
                    try:
                        import sentry_sdk
                        sentry_sdk.capture_exception(exc)
                    except Exception:
                        pass
                    text = json.dumps({"error": str(exc)})
                except (TimeoutError, Exception) as exc:
                    is_timeout = isinstance(exc, TimeoutError) or "timeout" in str(exc).lower()
                    if is_timeout:
                        print(f"GATEWAY: upstream timeout {slug}/{tool_name} after retries: {exc}", file=sys.stderr)
                        error_msg = (
                            f"Upstream MCP '{slug}' timed out after retries. "
                            f"The server may be overloaded or unreachable. "
                            f"Try again later or increase MCP_CALL_TIMEOUT (current: {TOOL_CALL_TIMEOUT}s)."
                        )
                    else:
                        print(f"GATEWAY: upstream error {slug}/{tool_name}: {exc}", file=sys.stderr)
                        error_msg = str(exc)
                    try:
                        import sentry_sdk
                        sentry_sdk.capture_exception(exc)
                    except Exception:
                        pass
                    text = json.dumps({"error": error_msg})
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                resp_bytes = len(text.encode("utf-8")) if text else 0
                _log_tool_call(
                    user_id, client_id, slug, tool_name,
                    credits_used=credit_cost,
                    duration_ms=elapsed_ms, response_bytes=resp_bytes,
                )

        else:
            text = json.dumps({"error": f"Unknown tool: {name}"})

        return [types.TextContent(type="text", text=text)]

    return server


def _get_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth[7:]


def _unauth_response(request: Request, detail: str = "Bearer token required") -> JSONResponse:
    """Return a 401 with OAuth discovery headers before the SSE transport starts."""
    issuer = get_settings().OAUTH_ISSUER_URL
    return JSONResponse(
        content={"error": "unauthorized", "error_description": detail},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="{issuer}",'
                f' error="invalid_token",'
                f' error_description="{detail}"'
            ),
            "Link": f'<{issuer}/.well-known/oauth-authorization-server>; rel="oauth-authorization-server"',
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

    if token_user_id != url_user_id:
        print(
            f"GATEWAY: user mismatch — token user_id={token_user_id!r} "
            f"vs URL user_id={url_user_id!r} on {request.method} {path}",
            file=sys.stderr,
        )
        response = _unauth_response(request, "Token does not match gateway URL")
        await response(scope, receive, send)
        return

    client_id = at.client_id
    print(
        f"GATEWAY: auth OK for user={token_user_id} client={client_id}, "
        f"{request.method} {path}",
        file=sys.stderr,
    )
    enabled_mcps = _load_enabled_mcps(token_user_id)
    mcp_server = _build_mcp_server(token_user_id, client_id, enabled_mcps)
    transport = StreamableHTTPServerTransport(mcp_session_id=None)

    response_started = False

    async def guarded_send(message):
        nonlocal response_started
        if message.get("type") == "http.response.start":
            response_started = True
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
    except Exception as exc:
        print(f"GATEWAY: exception: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        if not response_started:
            error = JSONResponse(
                content={"error": "internal_error", "error_description": str(exc)},
                status_code=500,
            )
            await error(scope, receive, send)
    finally:
        with anyio.move_on_after(2, shield=True):
            await transport.terminate()


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

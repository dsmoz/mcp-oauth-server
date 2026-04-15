from dotenv import load_dotenv
load_dotenv()  # Must be first — before any src imports that consume env vars

import os
import sys
import asyncio

# Initialise Sentry (no-op if SENTRY_DSN is not set)
_sentry_dsn = os.getenv("SENTRY_DSN")
if _sentry_dsn:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
    sentry_sdk.init(
        dsn=_sentry_dsn,
        traces_sample_rate=0.1,
        environment=os.getenv("RAILWAY_ENVIRONMENT", "development"),
        integrations=[StarletteIntegration(), FastApiIntegration()],
    )
    print("✅ Sentry error tracking enabled", file=sys.stderr)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from src.limiter import limiter
from src.oauth.routes import router as oauth_router
from src.admin.routes import router as admin_router
from src.portal.routes import router as portal_router
from src.gateway.routes import GatewayASGI
from src.gateway.rest_proxy import router as rest_proxy_router
from src.config import get_settings

app = FastAPI(
    title="DS-MOZ MCP OAuth Server",
    description="OAuth 2.0 authorization server for Claude Desktop custom connectors.",
    version="1.0.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — allow Zotero plugin (chrome:// origin) and browser-based clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def start_gateway_cleanup():
    from src.gateway.routes import start_cleanup_loop
    asyncio.create_task(start_cleanup_loop())


@app.on_event("startup")
async def startup_checks():
    import sys
    settings = get_settings()
    if settings.ADMIN_PASSWORD == "admin":
        print("WARNING: ADMIN_PASSWORD is set to default 'admin' — change before production use!", file=sys.stderr)
    if settings.INTROSPECT_SECRET in ("change-me", ""):
        print("WARNING: INTROSPECT_SECRET is set to default — change before production use!", file=sys.stderr)

    # Validate upstream MCP API keys at startup
    try:
        from src.db import get_db
        published_mcps = get_db().table("mcp_catalogue").select("slug, name, upstream_api_key, upstream_url").eq("is_published", True).execute().data or []
        for mcp in published_mcps:
            key = mcp.get("upstream_api_key") or ""
            if not key.strip():
                print(f"WARNING: Published MCP '{mcp['slug']}' ({mcp.get('name', '?')}) has no upstream_api_key configured. "
                      f"Upstream calls will fail with 401 if the server requires auth.", file=sys.stderr)
    except Exception as exc:
        print(f"WARNING: Could not validate MCP catalogue API keys at startup: {exc}", file=sys.stderr)

    # Log configured upstream timeout
    from src.gateway.upstream import TOOL_CALL_TIMEOUT
    print(f"INFO: Upstream MCP call timeout: {TOOL_CALL_TIMEOUT}s (set MCP_CALL_TIMEOUT env var to override)", file=sys.stderr)

    # Register Telegram webhook if configured
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_OWNER_CHAT_ID:
        from src import telegram as tg
        webhook_url = f"{settings.OAUTH_ISSUER_URL}/telegram/webhook"
        await tg.register_webhook(webhook_url)
        print(f"INFO: Telegram webhook registered at {webhook_url}", file=sys.stderr)
    else:
        print("INFO: Telegram not configured — consent will use password form fallback.", file=sys.stderr)

app.include_router(oauth_router)
app.include_router(admin_router)
app.include_router(portal_router)
app.include_router(rest_proxy_router)

# Serve portal static assets (brand icon, etc.)
_portal_static = Path(__file__).parent / "src" / "portal" / "static"
app.mount("/portal/static", StaticFiles(directory=str(_portal_static)), name="portal-static")


@app.get("/health")
async def health():
    return {"status": "ok"}


# Wrap with gateway ASGI middleware — intercepts /gateway/ requests before
# FastAPI so the MCP transport can own the full response lifecycle.
app = GatewayASGI(app)


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)

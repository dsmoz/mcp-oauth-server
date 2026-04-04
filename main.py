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
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from src.limiter import limiter
from src.oauth.routes import router as oauth_router
from src.admin.routes import router as admin_router
from src.portal.routes import router as portal_router
from src.gateway.routes import GatewayASGI
from src.config import get_settings

app = FastAPI(
    title="DS-MOZ MCP OAuth Server",
    description="OAuth 2.0 authorization server for Claude Desktop custom connectors.",
    version="1.0.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


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

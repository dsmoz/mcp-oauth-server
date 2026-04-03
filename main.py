from dotenv import load_dotenv
load_dotenv()  # Must be first — before any src imports that consume env vars

from fastapi import FastAPI
from src.oauth.routes import router as oauth_router
from src.admin.routes import router as admin_router
from src.config import get_settings

app = FastAPI(
    title="DS-MOZ MCP OAuth Server",
    description="OAuth 2.0 authorization server for Claude Desktop custom connectors.",
    version="1.0.0",
)


@app.on_event("startup")
async def startup_checks():
    import sys
    settings = get_settings()
    if settings.ADMIN_PASSWORD == "admin":
        print("WARNING: ADMIN_PASSWORD is set to default 'admin' — change before production use!", file=sys.stderr)
    if settings.INTROSPECT_SECRET in ("change-me", ""):
        print("WARNING: INTROSPECT_SECRET is set to default — change before production use!", file=sys.stderr)

app.include_router(oauth_router)
app.include_router(admin_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)

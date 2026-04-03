# DS-MOZ MCP OAuth Server

OAuth 2.0 authorization server for Claude Desktop custom connectors. Handles the full authorization code flow with PKCE and provides a `/introspect` endpoint that MCP servers call to validate tokens.

## Architecture

- **FastAPI** application
- **Supabase** backend (4 tables: oauth_clients, oauth_authorization_codes, oauth_access_tokens, oauth_refresh_tokens)
- **Railway** deployment via Dockerfile
- **Admin webapp** for client management (Jinja2 templates)

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/.well-known/openid-configuration` | GET | Discovery document |
| `/.well-known/oauth-authorization-server` | GET | Discovery document (alias) |
| `/authorize` | GET | Start authorization flow |
| `/authorize/consent` | GET/POST | Consent page |
| `/token` | POST | Token exchange |
| `/revoke` | POST | Token revocation |
| `/introspect` | POST | Token introspection (internal) |
| `/admin/` | GET | List clients |
| `/admin/clients/new` | GET | Create client form |
| `/admin/clients` | POST | Create client |
| `/admin/clients/{id}` | GET | Client detail |
| `/admin/clients/{id}/revoke` | POST | Revoke client tokens |

## Local Development

```bash
# Create virtual environment
uv venv
source .venv/bin/activate

# Install dependencies
uv pip install -e .

# Run
python main.py
```

## Environment Variables

Copy `.env.example` to `.env` and fill in values. For local dev, `.env` is pre-populated.

## Deployment (Railway)

1. Push to GitHub
2. Connect repo in Railway
3. Set environment variables (from `.env.example`)
4. Deploy — Railway uses the `Dockerfile`

## Admin Access

Navigate to `/admin/` — protected by HTTP Basic auth (`ADMIN_USERNAME` / `ADMIN_PASSWORD`).

## Token Introspection (for MCP servers)

```http
POST /introspect
X-Introspect-Secret: your-introspect-secret
Content-Type: application/json

{"token": "bearer-token-here"}
```

Returns `{"active": true, "client_id": "...", "scope": "mcp", "exp": 1234567890}`.

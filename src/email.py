"""
Transactional email via Brevo REST API.
Uses httpx — no extra dependency.
"""
from __future__ import annotations

import httpx

from src.config import get_settings

_BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"

_APPROVAL_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Your MCP Access Credentials</title>
<style>
  body {{ font-family: 'Segoe UI', Helvetica Neue, Arial, sans-serif; background: #f4f6f8; margin: 0; padding: 2rem; }}
  .card {{ background: #ffffff; border-radius: 10px; max-width: 560px; margin: 0 auto; padding: 2rem 2.5rem; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}
  .brand {{ font-size: 0.65rem; font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase; color: #FF5E00; margin-bottom: 1.5rem; }}
  h1 {{ font-size: 1.3rem; color: #0A1C20; margin: 0 0 0.5rem; }}
  p {{ color: #5A8A90; font-size: 0.95rem; line-height: 1.6; margin: 0.5rem 0; }}
  .credentials {{ background: #060E10; border-radius: 8px; padding: 1.25rem 1.5rem; margin: 1.5rem 0; }}
  .cred-row {{ margin-bottom: 0.75rem; }}
  .cred-label {{ font-size: 0.65rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: #FF5E00; }}
  .cred-value {{ font-family: 'Courier New', monospace; font-size: 0.9rem; color: #FFAE62; word-break: break-all; }}
  .warn {{ font-size: 0.8rem; color: #FFAE62; opacity: 0.8; margin-top: 0.5rem; }}
  .section {{ margin-top: 1.75rem; }}
  .section h2 {{ font-size: 1rem; color: #0A1C20; margin: 0 0 0.5rem; }}
  .code-block {{ background: #f0f7f8; border: 1px solid #d4e8ea; border-radius: 6px; padding: 0.9rem 1rem; font-family: 'Courier New', monospace; font-size: 0.8rem; color: #0A1C20; white-space: pre; overflow-x: auto; margin: 0.5rem 0 1rem; }}
  .footer {{ margin-top: 2rem; font-size: 0.75rem; color: #91BCC1; border-top: 1px solid #d4e8ea; padding-top: 1rem; }}
</style>
</head>
<body>
<div class="card">
  <div class="brand">
    <img src="https://res.cloudinary.com/dq2ajrfxl/image/upload/v1742962913/dsmoz_logos/dsmoz-logo-orange.png"
         alt="DS-MOZ Intelligence" width="180" height="45"
         style="display:block;margin-bottom:1.5rem">
  </div>
  <h1>Your MCP Access is Approved</h1>
  <p>Hi {contact_name}, your registration for <strong>{company_name}</strong> has been approved. Below are your credentials — keep them safe.</p>

  <div class="credentials">
    <div class="cred-row">
      <div class="cred-label">Client ID</div>
      <div class="cred-value">{client_id}</div>
    </div>
    <div class="cred-row">
      <div class="cred-label">Client Secret</div>
      <div class="cred-value">{client_secret}</div>
    </div>
    <div class="warn">&#9888; This is the only time your secret will be shown. Store it securely.</div>
  </div>

  <div class="section">
    <h2>Configure Claude Desktop</h2>
    <p>Add the following to your <code>claude_desktop_config.json</code>:</p>
    <div class="code-block">{claude_config}</div>
    <p>Config file location:<br>
      <strong>macOS:</strong> <code>~/Library/Application Support/Claude/claude_desktop_config.json</code><br>
      <strong>Windows:</strong> <code>%APPDATA%\\Claude\\claude_desktop_config.json</code>
    </p>
  </div>

  <div class="section">
    <h2>Configure ChatGPT (Custom GPT / Plugin)</h2>
    <p>Use these values when setting up an OAuth connection in your Custom GPT or GPT Action:</p>
    <div class="code-block">{chatgpt_config}</div>
  </div>

  <div class="section">
    <h2>Set Up Your Client Portal</h2>
    <p>Use the link below to create your portal login (valid for 24 hours, one-time use):</p>
    <p style="margin:0.75rem 0"><a href="{setup_url}" style="color:#FF5E00;font-weight:600;word-break:break-all">{setup_url}</a></p>
    <p>In the portal you can toggle which MCP tools you want access to and download your gateway configuration.</p>
  </div>

  <div class="footer">
    Questions? Reply to this email or contact your DS-MOZ Intelligence administrator.<br>
    &copy; DS-MOZ Intelligence
  </div>
</div>
</body>
</html>
"""

_CLAUDE_CONFIG_TEMPLATE = """\
{{
  "mcpServers": {{
    "{server_name}": {{
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-everything"],
      "env": {{
        "MCP_SERVER_URL": "{issuer_url}/mcp",
        "CLIENT_ID": "{client_id}",
        "CLIENT_SECRET": "{client_secret}",
        "OAUTH_TOKEN_URL": "{issuer_url}/oauth/token"
      }}
    }}
  }}
}}"""

_CHATGPT_CONFIG_TEMPLATE = """\
Authorization URL : {issuer_url}/oauth/authorize
Token URL         : {issuer_url}/oauth/token
Client ID         : {client_id}
Client Secret     : {client_secret}
Scope             : mcp"""


async def send_approval_email(
    contact_name: str,
    contact_email: str,
    company_name: str,
    client_id: str,
    raw_secret: str,
    issuer_url: str,
    setup_token: str = "",
) -> None:
    """Send credentials + setup instructions to the newly approved client."""
    settings = get_settings()
    if not settings.BREVO_API_KEY or not settings.BREVO_SENDER_EMAIL:
        import sys
        print("WARNING: Brevo not configured — skipping approval email", file=sys.stderr)
        return

    server_name = company_name.lower().replace(" ", "-")
    setup_url = f"{issuer_url}/portal/setup-password?token={setup_token}" if setup_token else f"{issuer_url}/portal/login"

    claude_config = _CLAUDE_CONFIG_TEMPLATE.format(
        server_name=server_name,
        issuer_url=issuer_url,
        client_id=client_id,
        client_secret=raw_secret,
    )
    chatgpt_config = _CHATGPT_CONFIG_TEMPLATE.format(
        issuer_url=issuer_url,
        client_id=client_id,
        client_secret=raw_secret,
    )

    html = _APPROVAL_HTML.format(
        contact_name=contact_name,
        company_name=company_name,
        client_id=client_id,
        client_secret=raw_secret,
        claude_config=claude_config,
        chatgpt_config=chatgpt_config,
        setup_url=setup_url,
    )

    payload = {
        "sender": {
            "name": settings.BREVO_SENDER_NAME,
            "email": settings.BREVO_SENDER_EMAIL,
        },
        "to": [{"name": contact_name, "email": contact_email}],
        "subject": f"Your DS-MOZ Intelligence MCP credentials — {company_name}",
        "htmlContent": html,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _BREVO_SEND_URL,
            json=payload,
            headers={
                "api-key": settings.BREVO_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

    if not resp.is_success:
        import sys
        print(f"WARNING: Brevo email failed ({resp.status_code}): {resp.text}", file=sys.stderr)

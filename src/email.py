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
<title>Your MCP Access is Ready</title>
<style>
  body {{ font-family: 'Segoe UI', Helvetica Neue, Arial, sans-serif; background: #f4f6f8; margin: 0; padding: 2rem; }}
  .card {{ background: #ffffff; border-radius: 10px; max-width: 560px; margin: 0 auto; padding: 2rem 2.5rem; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}
  h1 {{ font-size: 1.3rem; color: #0A1C20; margin: 0 0 0.5rem; }}
  p {{ color: #5A8A90; font-size: 0.95rem; line-height: 1.6; margin: 0.5rem 0; }}
  .section {{ margin-top: 1.75rem; }}
  .section h2 {{ font-size: 1rem; color: #0A1C20; margin: 0 0 0.5rem; }}
  .code-block {{ background: #f0f7f8; border: 1px solid #d4e8ea; border-radius: 6px; padding: 0.9rem 1rem; font-family: 'Courier New', monospace; font-size: 0.8rem; color: #0A1C20; white-space: pre; overflow-x: auto; margin: 0.5rem 0 1rem; }}
  .footer {{ margin-top: 2rem; font-size: 0.75rem; color: #91BCC1; border-top: 1px solid #d4e8ea; padding-top: 1rem; }}
</style>
</head>
<body>
<div class="card">
  <div style="margin-bottom:1.5rem">
    <img src="https://res.cloudinary.com/dq2ajrfxl/image/upload/v1742962913/dsmoz_logos/dsmoz-logo-orange.png"
         alt="DS-MOZ Intelligence" width="180" height="45"
         style="display:block">
  </div>
  <h1>Your MCP Access is Ready</h1>
  <p>Hi {contact_name}, your registration for <strong>{company_name}</strong> has been approved.</p>
  <p>First, set up your portal password using the link below — then log in to find your gateway URL and connect your AI assistant.</p>

  <div class="section">
    <h2>Step 1 — Set Up Your Portal</h2>
    <p>Use the link below to create your password (valid for 24 hours, one-time use):</p>
    <p style="margin:0.75rem 0"><a href="{setup_url}" style="color:#FF5E00;font-weight:600;word-break:break-all">{setup_url}</a></p>
  </div>

  <div class="section">
    <h2>Step 2 — Connect Claude Desktop or Cursor</h2>
    <p>After setting up your portal, copy your gateway URL and add it to your <code>claude_desktop_config.json</code>:</p>
    <div class="code-block">{claude_config}</div>
    <p style="font-size:0.8rem;color:#91BCC1">Config file location:<br>
      <strong>macOS:</strong> ~/Library/Application Support/Claude/claude_desktop_config.json<br>
      <strong>Windows:</strong> %APPDATA%\Claude\claude_desktop_config.json
    </p>
    <p style="font-size:0.8rem;color:#5A8A90;margin-top:0.5rem">Claude Desktop will prompt you to sign in via browser when you first connect.</p>
  </div>

  <div class="section">
    <h2>Step 3 — Connect Claude.ai (Web)</h2>
    <p>Go to <strong>Claude.ai → Settings → Connectors → Add custom connector</strong>, enter your gateway URL, and sign in when prompted.</p>
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
      "type": "sse",
      "url": "{gateway_url}"
    }}
  }}
}}"""


async def send_approval_email(
    contact_name: str,
    contact_email: str,
    company_name: str,
    client_id: str,
    issuer_url: str,
    setup_token: str = "",
) -> None:
    """Send setup instructions to the newly approved client."""
    settings = get_settings()
    if not settings.BREVO_API_KEY or not settings.BREVO_SENDER_EMAIL:
        import sys
        print("WARNING: Brevo not configured — skipping approval email", file=sys.stderr)
        return

    server_name = company_name.lower().replace(" ", "-")
    gateway_url = f"{issuer_url}/gateway/{client_id}"
    setup_url = f"{issuer_url}/portal/setup-password?token={setup_token}" if setup_token else f"{issuer_url}/portal/login"

    claude_config = _CLAUDE_CONFIG_TEMPLATE.format(
        server_name=server_name,
        gateway_url=gateway_url,
    )

    html = _APPROVAL_HTML.format(
        contact_name=contact_name,
        company_name=company_name,
        gateway_url=gateway_url,
        claude_config=claude_config,
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


_RESET_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reset Your Password</title>
<style>
  body {{ font-family: 'Segoe UI', Helvetica Neue, Arial, sans-serif; background: #f4f6f8; margin: 0; padding: 2rem; }}
  .card {{ background: #ffffff; border-radius: 10px; max-width: 480px; margin: 0 auto; padding: 2rem 2.5rem; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}
  .brand {{ margin-bottom: 1.5rem; }}
  h1 {{ font-size: 1.3rem; color: #0A1C20; margin: 0 0 0.5rem; }}
  p {{ color: #5A8A90; font-size: 0.95rem; line-height: 1.6; margin: 0.5rem 0; }}
  .btn {{ display: inline-block; margin: 1.5rem 0; padding: 0.75rem 2rem; background: #FF5E00; color: #ffffff; text-decoration: none; border-radius: 6px; font-weight: 700; font-size: 0.95rem; }}
  .link {{ font-size: 0.8rem; color: #91BCC1; word-break: break-all; }}
  .footer {{ margin-top: 2rem; font-size: 0.75rem; color: #91BCC1; border-top: 1px solid #d4e8ea; padding-top: 1rem; }}
</style>
</head>
<body>
<div class="card">
  <div class="brand">
    <img src="https://res.cloudinary.com/dq2ajrfxl/image/upload/v1742962913/dsmoz_logos/dsmoz-logo-orange.png"
         alt="DS-MOZ Intelligence" width="180" height="45" style="display:block">
  </div>
  <h1>Reset Your Password</h1>
  <p>Hi {contact_name}, we received a request to reset the password for your DS-MOZ Intelligence portal account.</p>
  <p>Click the button below to set a new password. This link is valid for 24 hours and can only be used once.</p>
  <a href="{reset_url}" class="btn">Reset Password</a>
  <p class="link">If the button doesn't work, paste this URL into your browser:<br>{reset_url}</p>
  <p style="margin-top:1rem;font-size:0.8rem;color:#91BCC1">If you did not request a password reset, you can safely ignore this email.</p>
  <div class="footer">&copy; DS-MOZ Intelligence</div>
</div>
</body>
</html>
"""


async def send_password_reset_email(
    contact_name: str,
    contact_email: str,
    reset_url: str,
) -> None:
    """Send password reset link to the portal user."""
    settings = get_settings()
    if not settings.BREVO_API_KEY or not settings.BREVO_SENDER_EMAIL:
        import sys
        print("WARNING: Brevo not configured — skipping password reset email", file=sys.stderr)
        return

    html = _RESET_HTML.format(contact_name=contact_name, reset_url=reset_url)

    payload = {
        "sender": {
            "name": settings.BREVO_SENDER_NAME,
            "email": settings.BREVO_SENDER_EMAIL,
        },
        "to": [{"name": contact_name, "email": contact_email}],
        "subject": "Reset your DS-MOZ Intelligence portal password",
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
        print(f"WARNING: Brevo reset email failed ({resp.status_code}): {resp.text}", file=sys.stderr)

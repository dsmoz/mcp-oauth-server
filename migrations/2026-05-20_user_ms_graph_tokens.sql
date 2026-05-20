-- Per-user Microsoft Graph OAuth tokens.
--
-- Stores the refresh_token (encrypted at the application layer via Fernet)
-- and the latest access_token + expiry. The gateway mints fresh access
-- tokens on demand when proxying requests to mcp-microsoft365.
--
-- Encryption: refresh_token_encrypted holds the Fernet ciphertext. The key
-- lives in env var GRAPH_TOKEN_ENCRYPTION_KEY (32-byte url-safe base64).
-- access_token is short-lived (≤1h) and stored as-is.

CREATE TABLE IF NOT EXISTS user_ms_graph_tokens (
    user_id                  text PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    refresh_token_encrypted  text NOT NULL,
    access_token             text NOT NULL,
    expires_at               timestamptz NOT NULL,
    scope                    text NOT NULL,
    ms_tenant_id             text,
    ms_user_principal_name   text,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_ms_graph_tokens_expires_at_idx
    ON user_ms_graph_tokens (expires_at);

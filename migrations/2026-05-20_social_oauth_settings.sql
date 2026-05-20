-- Admin-managed social sign-in credentials (Google + Microsoft).
-- Stored in admin_settings so an admin can rotate keys without redeploying.
-- Empty value → social.py falls back to env vars (GOOGLE_OAUTH_CLIENT_ID etc.).

INSERT INTO admin_settings (key, value, category, label, description, value_type) VALUES
  ('google_oauth_client_id',     '', 'social_auth', 'Google Client ID',     'OAuth 2.0 client ID from Google Cloud Console (APIs & Services → Credentials).', 'text'),
  ('google_oauth_client_secret', '', 'social_auth', 'Google Client Secret', 'OAuth 2.0 client secret. Stored in DB; rotate here without redeploy.',          'secret'),
  ('microsoft_oauth_client_id',     '', 'social_auth', 'Microsoft Client ID',     'Application (client) ID from Entra app registration.',                          'text'),
  ('microsoft_oauth_client_secret', '', 'social_auth', 'Microsoft Client Secret', 'Client secret value (not secret ID) from Entra → Certificates & secrets.',     'secret'),
  ('microsoft_oauth_tenant',        'common', 'social_auth', 'Microsoft Tenant',  'Entra tenant. Use "common" for multi-tenant + personal MS accounts, or a specific tenant GUID.', 'text')
ON CONFLICT (key) DO NOTHING;

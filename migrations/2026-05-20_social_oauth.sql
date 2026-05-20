-- Social sign-in (Google + Microsoft) support on users table.
-- Adds provider linkage columns + avatar. password_hash already nullable.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS oauth_provider text,
    ADD COLUMN IF NOT EXISTS oauth_sub      text,
    ADD COLUMN IF NOT EXISTS avatar_url     text;

-- One (provider, sub) maps to exactly one user.
CREATE UNIQUE INDEX IF NOT EXISTS users_oauth_provider_sub_uniq
    ON users (oauth_provider, oauth_sub)
    WHERE oauth_provider IS NOT NULL AND oauth_sub IS NOT NULL;

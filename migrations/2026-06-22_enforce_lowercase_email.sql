-- 2026-06-22_enforce_lowercase_email.sql
--
-- Enforce canonical (lowercase) emails on the gateway users table.
--
-- Context: the scholar shared-wallet merge (src/gateway/jwt_auth.py
-- resolve_or_create_user) matches an incoming Supabase identity to an existing
-- gateway wallet by lowercased email. A stray mixed-case row would be missed by
-- that lookup and silently fork into a second wallet for the same person.
--
-- App-side normalization is the primary guard (src/users/provider.create_user
-- now trims + lowercases at the single insert chokepoint). This CHECK is the
-- DB-level backstop so the invariant holds even if a future write path bypasses
-- the provider.
--
-- Applied to dsmoz-intel (bwbghsnnrszdcmwqzjwv) via supabase apply_migration.
-- Pre-checked safe: 0 existing rows violated (email = lower(email) for all).

ALTER TABLE public.users
  ADD CONSTRAINT users_email_lowercase CHECK (email = lower(email));

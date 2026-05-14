-- Public OAuth clients — single shared client_id authorised by many users
--
-- A "public" client (e.g. the dsmoz-academia web app) is registered once and
-- then authorised by many distinct end-users. Each user receives an access
-- token bound to *their* user_id, not to whoever first claimed the client.
--
-- Two schema changes are needed:
--   1. oauth_clients.is_public_client — flag clients exempt from the
--      one-user-claim flow. user_id stays NULL on these rows.
--   2. oauth_authorization_codes.user_id — capture the authorising user at
--      consent time so token exchange can bind tokens to that user rather
--      than to the client's claimer.

ALTER TABLE oauth_clients
    ADD COLUMN IF NOT EXISTS is_public_client boolean NOT NULL DEFAULT false;

ALTER TABLE oauth_authorization_codes
    ADD COLUMN IF NOT EXISTS user_id text;

-- Public clients must never have a claimed user. Defence in depth alongside
-- the application-level guard in claim_unclaimed_client().
CREATE OR REPLACE FUNCTION enforce_public_client_unclaimed()
RETURNS trigger AS $$
BEGIN
    IF NEW.is_public_client AND NEW.user_id IS NOT NULL THEN
        RAISE EXCEPTION 'public OAuth clients must not have user_id set';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_public_client_unclaimed ON oauth_clients;
CREATE TRIGGER trg_public_client_unclaimed
    BEFORE INSERT OR UPDATE ON oauth_clients
    FOR EACH ROW EXECUTE FUNCTION enforce_public_client_unclaimed();

-- Make the DCR fingerprint unique constraint partial: only unclaimed rows must be unique.
-- Rationale: once a client is claimed by user A, user B must be able to DCR-register
-- with the same client_name (same fingerprint) and get their own claimable row.

alter table oauth_clients drop constraint if exists uq_dcr_fingerprint;
drop index if exists uq_dcr_fingerprint;

create unique index if not exists uq_dcr_fingerprint_unclaimed
  on oauth_clients (dcr_fingerprint)
  where user_id is null and dcr_fingerprint is not null;

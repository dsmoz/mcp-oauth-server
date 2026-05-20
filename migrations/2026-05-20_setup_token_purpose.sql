-- portal_setup_tokens.purpose
--
-- Distinguishes initial-setup tokens from password-reset tokens so a stale
-- "set up your account" link cannot be used to overwrite an already-set
-- password, and a reset link cannot be used to (re)activate an inactive
-- account.
--
-- Allowed values: 'setup' | 'reset'.
-- Legacy rows default to 'setup' (the original semantics).

alter table portal_setup_tokens
  add column if not exists purpose text not null default 'setup';

alter table portal_setup_tokens
  add constraint portal_setup_tokens_purpose_chk
  check (purpose in ('setup', 'reset'));

create index if not exists portal_setup_tokens_purpose_idx
  on portal_setup_tokens(purpose);

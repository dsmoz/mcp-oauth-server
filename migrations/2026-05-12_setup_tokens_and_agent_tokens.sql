-- 1. portal_setup_tokens.user_id
alter table portal_setup_tokens add column if not exists user_id text;
update portal_setup_tokens pst
   set user_id = oc.user_id
  from oauth_clients oc
 where pst.user_id is null
   and pst.client_id = oc.client_id;
alter table portal_setup_tokens alter column client_id drop not null;
create index if not exists portal_setup_tokens_user_id_idx
  on portal_setup_tokens(user_id);

-- 2. user_agent_tokens
create table if not exists user_agent_tokens (
  id           uuid primary key default gen_random_uuid(),
  user_id      text not null references users(user_id) on delete cascade,
  label        text not null,
  token_hash   text not null unique,
  prefix       text not null,
  created_at   timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at   timestamptz,
  expires_at   timestamptz
);
create index if not exists user_agent_tokens_user_idx
  on user_agent_tokens(user_id) where revoked_at is null;

-- ─────────────────────────────────────────────────────────────────────────────
-- Migration: Decouple users from oauth_clients (multi-device tenancy)
-- Date: 2026-04-14
-- Plan: .claude/plans/polymorphic-stirring-walrus.md
--
-- Background:
--   oauth_clients currently conflates the human tenant (credits, MCP toolbox,
--   gateway namespace, password) with the OAuth client app (one row per
--   device/AI app). RFC 7591 DCR creates a new oauth_clients row per
--   client_name fingerprint, so the same human ends up with multiple
--   disjoint tenants when connecting from Claude Code, ChatGPT, etc.
--
--   This migration introduces a `users` table as the tenancy key. Each
--   existing oauth_clients row maps one-to-one to a new users row to
--   preserve current isolation. Going forward, DCR creates clients with
--   user_id = NULL ("unclaimed"); the first user to complete /authorize
--   adopts them.
-- ─────────────────────────────────────────────────────────────────────────────

begin;

-- ── 1. New users table (the tenant) ──────────────────────────────────────────
create table if not exists public.users (
    user_id               text primary key,
    email                 text unique not null,
    password_hash         text,
    display_name          text,
    credit_balance        numeric default 0,
    allowed_mcp_resources text[]  default '{}',
    is_active             boolean default false,
    created_at            timestamptz default now(),
    updated_at            timestamptz default now()
);

create index if not exists idx_users_email on public.users(email);

-- ── 2. Add user_id + claimed_at to oauth_clients ─────────────────────────────
alter table public.oauth_clients
    add column if not exists user_id    text references public.users(user_id) on delete cascade,
    add column if not exists claimed_at timestamptz;

create index if not exists idx_oauth_clients_user_id   on public.oauth_clients(user_id);
create index if not exists idx_oauth_clients_unclaimed on public.oauth_clients(created_at)
    where user_id is null;

-- ── 3. Add user_id to tokens & usage logs ────────────────────────────────────
alter table public.oauth_access_tokens
    add column if not exists user_id text references public.users(user_id) on delete cascade;
alter table public.oauth_refresh_tokens
    add column if not exists user_id text references public.users(user_id) on delete cascade;
alter table public.oauth_usage_logs
    add column if not exists user_id text;

create index if not exists idx_access_tokens_user_id  on public.oauth_access_tokens(user_id);
create index if not exists idx_refresh_tokens_user_id on public.oauth_refresh_tokens(user_id);
create index if not exists idx_usage_logs_user_id     on public.oauth_usage_logs(user_id);

-- ── 4. One-to-one backfill ───────────────────────────────────────────────────
-- Each existing oauth_clients row → one users row. Email collisions are
-- resolved by appending a slice of the client_id; operator can merge later.

insert into public.users (
    user_id, email, password_hash, display_name,
    credit_balance, allowed_mcp_resources, is_active
)
select
    'usr_' || substring(md5(client_id) for 16) as user_id,
    case
        when portal_username is not null
             and not exists (
                select 1 from public.users u
                where u.email = portal_username
             )
            then portal_username
        when portal_username is not null
            then portal_username || '+' || substring(md5(client_id) for 6)
        when created_by is not null
             and not exists (
                select 1 from public.users u
                where u.email = created_by
             )
            then created_by
        when created_by is not null
            then created_by || '+' || substring(md5(client_id) for 6)
        else client_id || '@unclaimed.local'
    end as email,
    portal_password_hash,
    client_name,
    coalesce(credit_balance, 0),
    coalesce(allowed_mcp_resources, '{}'::text[]),
    coalesce(is_active, false)
from public.oauth_clients
on conflict (user_id) do nothing;

-- Backfill oauth_clients.user_id and claimed_at
update public.oauth_clients oc
set user_id    = 'usr_' || substring(md5(oc.client_id) for 16),
    claimed_at = coalesce(oc.created_at, now())
where oc.user_id is null;

-- Backfill tokens and usage logs
update public.oauth_access_tokens t
set user_id = oc.user_id
from public.oauth_clients oc
where oc.client_id = t.client_id
  and t.user_id is null;

update public.oauth_refresh_tokens t
set user_id = oc.user_id
from public.oauth_clients oc
where oc.client_id = t.client_id
  and t.user_id is null;

update public.oauth_usage_logs u
set user_id = oc.user_id
from public.oauth_clients oc
where oc.client_id = u.client_id
  and u.user_id is null;

-- ── 5. deduct_credits RPC: now operates on users ─────────────────────────────
-- Replaces deduct_credits(p_client_id text, p_amount numeric).
-- Atomic decrement; raises if balance would go negative.

create or replace function public.deduct_credits_user(
    p_user_id text,
    p_amount  numeric
) returns numeric
language plpgsql
security definer
as $$
declare
    new_balance numeric;
begin
    update public.users
    set credit_balance = credit_balance - p_amount,
        updated_at     = now()
    where user_id = p_user_id
      and credit_balance >= p_amount
    returning credit_balance into new_balance;

    if new_balance is null then
        raise exception 'INSUFFICIENT_CREDITS' using errcode = 'P0001';
    end if;

    return new_balance;
end;
$$;

grant execute on function public.deduct_credits_user(text, numeric) to anon, authenticated, service_role;

commit;

-- ─────────────────────────────────────────────────────────────────────────────
-- Verification queries (run manually after applying):
--
--   select count(*) as users_count from public.users;
--   select count(*) as clients_count from public.oauth_clients;
--   -- The two counts must be equal after one-to-one backfill.
--
--   select count(*) from public.oauth_clients where user_id is null;
--   -- Must be 0.
--
--   select count(*) from public.oauth_access_tokens where user_id is null;
--   select count(*) from public.oauth_refresh_tokens where user_id is null;
--   -- Both must be 0 (assuming all tokens have a valid client_id FK).
-- ─────────────────────────────────────────────────────────────────────────────

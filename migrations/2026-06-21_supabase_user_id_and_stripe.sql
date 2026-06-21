-- Migration: scholar-first identity + Stripe + caller_kind
-- Date: 2026-06-21
-- Applied via Supabase MCP apply_migration on bwbghsnnrszdcmwqzjwv.
-- Spec: ../scholar/docs/superpowers/specs/2026-06-21-scholar-first-identity-billing-design.md
--
-- Adds:
--   1. users.supabase_user_id (nullable UUID, unique) — scholar Supabase bridge
--   2. credit_topup_requests.{payment_provider,stripe_session_id,stripe_payment_intent}
--   3. mcp_tokens table (RLS-enabled, service-role only)
--   4. oauth_usage_logs.caller_kind + .request_id (unique idempotency)

alter table public.users
    add column if not exists supabase_user_id uuid;
create unique index if not exists uq_users_supabase_user_id
    on public.users(supabase_user_id)
    where supabase_user_id is not null;

alter table public.credit_topup_requests
    add column if not exists payment_provider     text,
    add column if not exists stripe_session_id    text,
    add column if not exists stripe_payment_intent text;
create unique index if not exists uq_credit_topup_stripe_session
    on public.credit_topup_requests(stripe_session_id)
    where stripe_session_id is not null;

create table if not exists public.mcp_tokens (
    token_id       uuid primary key default gen_random_uuid(),
    user_id        text not null references public.users(user_id) on delete cascade,
    token_hash     text not null unique,
    scope          text not null,
    label          text,
    expires_at     timestamptz,
    created_at     timestamptz not null default now(),
    revoked_at     timestamptz
);
create index if not exists idx_mcp_tokens_user on public.mcp_tokens(user_id);
create index if not exists idx_mcp_tokens_active
    on public.mcp_tokens(user_id) where revoked_at is null;
alter table public.mcp_tokens enable row level security;

alter table public.oauth_usage_logs
    add column if not exists caller_kind text;
create index if not exists idx_oauth_usage_caller_kind
    on public.oauth_usage_logs(caller_kind) where caller_kind is not null;

alter table public.oauth_usage_logs
    add column if not exists request_id text;
create unique index if not exists uq_oauth_usage_request_id
    on public.oauth_usage_logs(request_id) where request_id is not null;

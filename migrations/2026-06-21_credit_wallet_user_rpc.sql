-- 2026-06-21: credit_wallet_user(p_user_id text, p_amount numeric) RPC
--
-- Adds credits to a user's balance. Used by the Stripe webhook handler
-- (src/gateway/stripe_webhook.py) on checkout.session.completed.
-- Counterpart to the existing settle_credits_user() function that debits.

create or replace function public.credit_wallet_user(p_user_id text, p_amount numeric)
returns numeric
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  new_bal numeric;
begin
  update public.users
     set credit_balance = coalesce(credit_balance, 0) + p_amount,
         updated_at = now()
   where user_id = p_user_id
   returning credit_balance into new_bal;
  return new_bal;
end$$;

revoke all on function public.credit_wallet_user(text, numeric) from public;

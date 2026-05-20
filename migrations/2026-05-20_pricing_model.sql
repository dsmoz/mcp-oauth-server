-- Pricing model v1 — cost-based per-call billing
-- See: _ADMIN/Reference-Documents/2026-05-20_saas-mcp-pricing-model.md
-- Adds:
--   pricing_config   (singleton: tax, margin, usd_per_credit)
--   compute_rates    (Railway compute USD rates)
--   model_prices     (LLM USD per 1k tokens)
--   mcp_cost_profile (per-MCP overrides)
--   oauth_usage_logs extensions (cost breakdown columns)
--   topup_packages.auto_grant_on_signup flag
--   users.signup_grant_at timestamp (one-time guard)

BEGIN;

-- ── pricing_config ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pricing_config (
    id                    smallint PRIMARY KEY CHECK (id = 1),
    withholding_rate      numeric(5,4)  NOT NULL DEFAULT 0.0000,
    margin_rate           numeric(5,4)  NOT NULL DEFAULT 0.6000,
    iva_rate              numeric(5,4)  NOT NULL DEFAULT 0.1600,
    usd_per_credit        numeric(10,8) NOT NULL DEFAULT 0.01000000,
    fx_usd_mzn            numeric(8,2)  NOT NULL DEFAULT 64.00,
    min_balance_to_call   numeric(8,2)  NOT NULL DEFAULT 1.0,
    updated_at            timestamptz   NOT NULL DEFAULT now(),
    updated_by            text
);

INSERT INTO pricing_config (id, withholding_rate, margin_rate, iva_rate, usd_per_credit, fx_usd_mzn, min_balance_to_call)
VALUES (1, 0.0000, 0.6000, 0.1600, 0.01000000, 64.00, 1.0)
ON CONFLICT (id) DO NOTHING;

-- ── compute_rates ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS compute_rates (
    name                       text PRIMARY KEY,
    usd_per_vcpu_sec           numeric(12,10) NOT NULL,
    usd_per_gb_egress          numeric(8,6)   NOT NULL,
    credit_base_per_call_usd   numeric(8,6)   NOT NULL DEFAULT 0.0001,
    notes                      text,
    updated_at                 timestamptz    NOT NULL DEFAULT now()
);

INSERT INTO compute_rates (name, usd_per_vcpu_sec, usd_per_gb_egress, credit_base_per_call_usd, notes)
VALUES
    ('default',  0.0000077, 0.05, 0.0001, 'Railway Hobby/Pro baseline. Refine from invoice review.'),
    ('high-mem', 0.0000154, 0.05, 0.0001, 'Memory-heavy services (2× vCPU rate as placeholder).')
ON CONFLICT (name) DO NOTHING;

-- ── model_prices ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_prices (
    model                     text PRIMARY KEY,
    provider                  text NOT NULL,
    input_per_1k_usd          numeric(10,6) NOT NULL,
    output_per_1k_usd         numeric(10,6) NOT NULL,
    cached_input_per_1k_usd   numeric(10,6),
    source_url                text,
    updated_at                timestamptz NOT NULL DEFAULT now()
);

-- Seed common models. Refresh from OpenRouter /api/v1/models via admin button.
INSERT INTO model_prices (model, provider, input_per_1k_usd, output_per_1k_usd, cached_input_per_1k_usd, source_url) VALUES
    ('claude-opus-4-7',                 'anthropic',  0.015,   0.075,   0.0015,  'https://docs.anthropic.com/en/docs/about-claude/pricing'),
    ('claude-sonnet-4-6',               'anthropic',  0.003,   0.015,   0.0003,  'https://docs.anthropic.com/en/docs/about-claude/pricing'),
    ('claude-haiku-4-5-20251001',       'anthropic',  0.001,   0.005,   0.0001,  'https://docs.anthropic.com/en/docs/about-claude/pricing'),
    ('gpt-4o',                          'openai',     0.0025,  0.010,   0.00125, 'https://openai.com/api/pricing/'),
    ('gpt-4o-mini',                     'openai',     0.00015, 0.0006,  0.000075,'https://openai.com/api/pricing/'),
    ('openrouter/anthropic/claude-sonnet-4.6', 'openrouter', 0.003, 0.015, 0.0003, 'https://openrouter.ai/models')
ON CONFLICT (model) DO NOTHING;

-- ── mcp_cost_profile ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mcp_cost_profile (
    mcp_slug                text PRIMARY KEY REFERENCES mcp_catalogue(slug) ON DELETE CASCADE,
    compute_rate_name       text NOT NULL DEFAULT 'default' REFERENCES compute_rates(name),
    llm_margin_override     numeric(5,4),
    fixed_surcharge_usd     numeric(8,6) NOT NULL DEFAULT 0,
    notes                   text,
    updated_at              timestamptz NOT NULL DEFAULT now()
);

-- Default profile for every published MCP. Idempotent.
INSERT INTO mcp_cost_profile (mcp_slug, compute_rate_name)
SELECT slug, 'default' FROM mcp_catalogue
ON CONFLICT (mcp_slug) DO NOTHING;

-- ── oauth_usage_logs extensions ───────────────────────────────────────────────
ALTER TABLE oauth_usage_logs ADD COLUMN IF NOT EXISTS compute_usd     numeric(12,8);
ALTER TABLE oauth_usage_logs ADD COLUMN IF NOT EXISTS llm_usd         numeric(12,8);
ALTER TABLE oauth_usage_logs ADD COLUMN IF NOT EXISTS raw_usd         numeric(12,8);
ALTER TABLE oauth_usage_logs ADD COLUMN IF NOT EXISTS sell_usd        numeric(12,8);
ALTER TABLE oauth_usage_logs ADD COLUMN IF NOT EXISTS credits_charged numeric(10,4);
ALTER TABLE oauth_usage_logs ADD COLUMN IF NOT EXISTS model_used      text;
ALTER TABLE oauth_usage_logs ADD COLUMN IF NOT EXISTS input_tokens    int;
ALTER TABLE oauth_usage_logs ADD COLUMN IF NOT EXISTS output_tokens   int;

-- ── topup_packages auto-grant flag ────────────────────────────────────────────
ALTER TABLE topup_packages ADD COLUMN IF NOT EXISTS auto_grant_on_signup boolean NOT NULL DEFAULT false;

-- Update existing packages to v1.1 design and add Power + Trial.
-- Match by name (idempotent).
UPDATE topup_packages SET
    price_amount = 650,  credits = 1000,  tag = 'Individual',          is_featured = false, sort_order = 1
WHERE name = 'Starter';

UPDATE topup_packages SET
    price_amount = 2500, credits = 4500,  tag = 'Most popular',        is_featured = true,  sort_order = 2
WHERE name = 'Pro';

UPDATE topup_packages SET
    price_amount = 34000, credits = 60000, tag = 'Team / heavy automation', is_featured = false, sort_order = 4
WHERE name = 'Enterprise';

-- Insert Power pack if missing.
INSERT INTO topup_packages (name, price_amount, currency, credits, tag, is_featured, is_published, sort_order)
SELECT 'Power', 7500, 'MZN', 15000, 'For content teams', false, true, 3
WHERE NOT EXISTS (SELECT 1 FROM topup_packages WHERE name = 'Power');

-- Insert Trial pack (free, signup grant) if missing.
INSERT INTO topup_packages (name, price_amount, currency, credits, tag, is_featured, is_published, sort_order, auto_grant_on_signup)
SELECT 'Trial', 0, 'MZN', 100, 'Try DS-MOZ Connect', false, false, 0, true
WHERE NOT EXISTS (SELECT 1 FROM topup_packages WHERE name = 'Trial');

-- ── users.signup_grant_at ─────────────────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_grant_at timestamptz;

-- ── Deprecation marker on mcp_catalogue.credit_cost_per_call ──────────────────
-- Keep column for one release cycle. Gateway code switches to formula-based.
COMMENT ON COLUMN mcp_catalogue.credit_cost_per_call IS
    'DEPRECATED 2026-05-20: cost is now computed from compute_rates + model_prices + pricing_config. See pricing model doc. Column kept for one release cycle then dropped.';

COMMIT;

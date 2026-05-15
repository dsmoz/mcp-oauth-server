-- Admin-configurable top-up packages

CREATE TABLE IF NOT EXISTS topup_packages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    price_amount numeric(10,2) NOT NULL,
    currency text NOT NULL DEFAULT 'USD',
    credits integer NOT NULL,
    tag text DEFAULT '',
    is_featured boolean NOT NULL DEFAULT false,
    is_published boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

-- Seed only if table is empty (idempotent)
INSERT INTO topup_packages (name, price_amount, currency, credits, tag, is_featured, sort_order)
SELECT * FROM (VALUES
    ('Starter'::text,    10::numeric,  'USD'::text, 10,  '~5 days of use'::text, false, 0),
    ('Pro'::text,        40::numeric,  'USD'::text, 50,  'most popular'::text,   true,  1),
    ('Enterprise'::text, 150::numeric, 'USD'::text, 200, 'best value'::text,     false, 2)
) AS seed(name, price_amount, currency, credits, tag, is_featured, sort_order)
WHERE NOT EXISTS (SELECT 1 FROM topup_packages);

-- Snapshot fields on credit_topup_requests
ALTER TABLE credit_topup_requests
    ADD COLUMN IF NOT EXISTS package_id uuid REFERENCES topup_packages(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS price_amount numeric(10,2),
    ADD COLUMN IF NOT EXISTS currency text;

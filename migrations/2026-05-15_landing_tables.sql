-- Landing page: featured flag, testimonials, partners, hero image setting

-- Featured flag on existing catalogue table
ALTER TABLE mcp_catalogue ADD COLUMN IF NOT EXISTS is_featured BOOLEAN DEFAULT FALSE;

-- Lucide icon name for carousel display
ALTER TABLE mcp_catalogue ADD COLUMN IF NOT EXISTS icon TEXT;

-- Testimonials
CREATE TABLE IF NOT EXISTS landing_testimonials (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  quote TEXT NOT NULL,
  author_name TEXT NOT NULL,
  author_role TEXT,
  author_org TEXT,
  author_initials TEXT,
  sort_order INT DEFAULT 0,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Partners / logos strip
CREATE TABLE IF NOT EXISTS landing_partners (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  logo_url TEXT,
  website_url TEXT,
  sort_order INT DEFAULT 0,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed initial testimonials
INSERT INTO landing_testimonials (quote, author_name, author_role, author_org, author_initials, sort_order)
VALUES
  (
    'DS-MOZ Connect transformou a forma como a nossa equipa acede às ferramentas de IA. Em minutos tínhamos o Claude ligado a todos os nossos sistemas de informação de saúde.',
    'João Silva',
    'Director de Sistemas de Informação',
    'MISAU',
    'JS',
    1
  ),
  (
    'A integração com o MCP Gateway da DS-MOZ poupou-nos semanas de desenvolvimento. A autenticação e a gestão de créditos funcionam de forma transparente.',
    'Ana Machava',
    'Responsável de Inovação Digital',
    'FDC',
    'AM',
    2
  )
ON CONFLICT DO NOTHING;

-- Seed initial partners
INSERT INTO landing_partners (name, logo_url, website_url, sort_order)
VALUES
  ('MISAU',   NULL, NULL, 1),
  ('FDC',     NULL, NULL, 2),
  ('USAID',   NULL, NULL, 3),
  ('JHPIEGO', NULL, NULL, 4),
  ('VIVA+',   NULL, NULL, 5),
  ('UNFPA',   NULL, NULL, 6)
ON CONFLICT DO NOTHING;

-- Hero image setting (category: branding)
INSERT INTO admin_settings (key, value, label, description, category, value_type)
VALUES (
  'landing_hero_image_url',
  '',
  'Hero Image URL',
  'Cloudflare Images URL for the landing page hero background. Leave empty for flat dark teal.',
  'branding',
  'text'
)
ON CONFLICT (key) DO NOTHING;

-- Migration 016: Promotional codes system
CREATE TABLE IF NOT EXISTS billing.promo_codes (
  code TEXT PRIMARY KEY,
  discount_type TEXT NOT NULL CHECK (discount_type IN ('percentage', 'fixed_amount')),
  discount_value DECIMAL(20, 6) NOT NULL CHECK (discount_value > 0),
  max_uses INTEGER CHECK (
    max_uses IS NULL
    OR max_uses > 0
  ),
  current_uses INTEGER NOT NULL DEFAULT 0 CHECK (current_uses >= 0),
  valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  valid_until TIMESTAMPTZ,
  active BOOLEAN NOT NULL DEFAULT true,
  applicable_packages TEXT [] DEFAULT ARRAY [] :: TEXT [],
  description TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT check_max_uses CHECK (
    max_uses IS NULL
    OR current_uses <= max_uses
  ),
  CONSTRAINT check_valid_dates CHECK (
    valid_until IS NULL
    OR valid_from < valid_until
  )
);

CREATE INDEX idx_promo_codes_active ON billing.promo_codes (active, code);

CREATE INDEX idx_promo_codes_valid ON billing.promo_codes (valid_from, valid_until)
WHERE
  active = true;

CREATE OR REPLACE FUNCTION billing.update_promo_codes_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_promo_codes_updated_at BEFORE
UPDATE
  ON billing.promo_codes FOR EACH ROW EXECUTE FUNCTION billing.update_promo_codes_updated_at();

COMMENT ON TABLE billing.promo_codes IS 'Promotional discount codes';

COMMENT ON COLUMN billing.promo_codes.discount_type IS 'percentage or fixed_amount';

COMMENT ON COLUMN billing.promo_codes.discount_value IS 'For percentage: 0.10 = 10% off, for fixed: dollar amount';

COMMENT ON COLUMN billing.promo_codes.applicable_packages IS 'Empty array means code works for all packages';

INSERT INTO
  billing.promo_codes (
    code,
    discount_type,
    discount_value,
    max_uses,
    description,
    valid_until
  )
VALUES
  (
    'LAUNCH20',
    'percentage',
    0.20,
    NULL,
    '20% off launch promotion',
    NOW() + INTERVAL '30 days'
  ),
  (
    'SAVE10',
    'fixed_amount',
    10.00,
    1000,
    '$10 credit for new users',
    NOW() + INTERVAL '90 days'
  ),
  (
    'STUDENT50',
    'percentage',
    0.50,
    NULL,
    '50% off for students (stackable with tier)',
    NULL
  ) ON CONFLICT (code) DO NOTHING;
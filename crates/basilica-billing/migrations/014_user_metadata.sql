-- Migration 014: User metadata for tier-based pricing
CREATE TABLE IF NOT EXISTS billing.user_metadata (
  user_id TEXT PRIMARY KEY,
  user_tier TEXT NOT NULL DEFAULT 'standard' CHECK (
    user_tier IN ('standard', 'student', 'enterprise', 'custom')
  ),
  discount_percentage DECIMAL(5, 4) CHECK (
    discount_percentage >= 0
    AND discount_percentage <= 1
  ),
  promo_codes TEXT [] DEFAULT ARRAY [] :: TEXT [],
  tier_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  custom_attributes JSONB DEFAULT '{}' :: jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_user_metadata_tier ON billing.user_metadata (user_tier);

CREATE INDEX idx_user_metadata_updated ON billing.user_metadata (updated_at);

CREATE OR REPLACE FUNCTION billing.update_user_metadata_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_user_metadata_updated_at BEFORE
UPDATE
  ON billing.user_metadata FOR EACH ROW EXECUTE FUNCTION billing.update_user_metadata_updated_at();

COMMENT ON TABLE billing.user_metadata IS 'User tier and discount information for pricing';

COMMENT ON COLUMN billing.user_metadata.user_tier IS 'User tier: standard, student, enterprise, custom';

COMMENT ON COLUMN billing.user_metadata.discount_percentage IS 'Automatic discount percentage (0.20 = 20% off)';

COMMENT ON COLUMN billing.user_metadata.promo_codes IS 'Array of applied promo codes';
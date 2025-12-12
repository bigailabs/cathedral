-- VIP Rental Support
-- Add is_vip flag column for VIP machine tracking
-- VIP machines use provider_instance_id with 'vip:' prefix (e.g., 'vip:machine-123')

-- Add is_vip flag column (default false for existing rows)
ALTER TABLE secure_cloud_rentals ADD COLUMN IF NOT EXISTS is_vip BOOLEAN NOT NULL DEFAULT FALSE;

-- Index on is_vip for efficient VIP rental queries
CREATE INDEX IF NOT EXISTS idx_secure_cloud_rentals_is_vip
    ON secure_cloud_rentals(is_vip)
    WHERE is_vip = TRUE;

-- Index on provider_instance_id for VIP lookups (matches 'vip:%' pattern)
CREATE INDEX IF NOT EXISTS idx_secure_cloud_rentals_provider_instance_id_vip
    ON secure_cloud_rentals(provider_instance_id)
    WHERE provider_instance_id LIKE 'vip:%';

COMMENT ON COLUMN secure_cloud_rentals.is_vip IS 'TRUE if this is a VIP rental (managed by sheet, cannot be stopped by user). VIP machines use provider_instance_id with vip: prefix';

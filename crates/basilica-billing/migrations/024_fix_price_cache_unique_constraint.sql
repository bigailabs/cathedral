-- Migration 024: Fix price_cache unique constraint and remove unused columns
-- This migration fixes duplicate row issue by removing columns with NULL values
-- and updating the UNIQUE constraint to use source instead of provider/location

-- ===========================================================================
-- 1. Drop existing unique constraint and indexes that reference removed columns
-- ===========================================================================

-- Drop the existing unique constraint
ALTER TABLE billing.price_cache
    DROP CONSTRAINT IF EXISTS price_cache_gpu_model_provider_location_is_spot_key;

-- ===========================================================================
-- 2. Remove unused columns
-- ===========================================================================

-- Remove provider, location, and instance_name columns
-- These are always NULL for aggregated prices and cause duplicate row issues
ALTER TABLE billing.price_cache
    DROP COLUMN IF EXISTS provider,
    DROP COLUMN IF EXISTS location,
    DROP COLUMN IF EXISTS instance_name;

-- ===========================================================================
-- 3. Remove duplicate rows before adding constraint
-- ===========================================================================

-- Delete duplicate rows, keeping only the most recent one (by created_at)
-- This is necessary because existing data may have duplicates that would
-- violate the new unique constraint
DELETE FROM billing.price_cache
WHERE id IN (
    SELECT id
    FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY gpu_model, source, is_spot
                   ORDER BY created_at DESC
               ) as row_num
        FROM billing.price_cache
    ) t
    WHERE row_num > 1
);

-- ===========================================================================
-- 4. Add new unique constraint based on source
-- ===========================================================================

-- Create new unique constraint: one price per (gpu_model, source, is_spot)
-- This prevents duplicates for aggregated prices like "aggregated_average"
ALTER TABLE billing.price_cache
    ADD CONSTRAINT price_cache_gpu_model_source_is_spot_key
    UNIQUE(gpu_model, source, is_spot);

-- Update table comment
COMMENT ON TABLE billing.price_cache IS
    'Stores cached GPU prices from external sources with expiration. Unique per (gpu_model, source, is_spot).';

COMMENT ON COLUMN billing.price_cache.source IS
    'Source of the price (e.g., "aggregated_average", "aggregated_minimum", "marketplace")';

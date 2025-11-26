-- Migration 027: Marketplace-2-Compute Refactoring
-- This migration transitions from package-based pricing to direct marketplace pricing
--
-- Changes:
-- 1. Add marketplace pricing fields to rentals table
-- 2. Remove package_id dependency from rentals
-- 3. Drop all package and dynamic pricing infrastructure

-- ============================================================================
-- PART 1: Add marketplace pricing fields to rentals table
-- ============================================================================

ALTER TABLE billing.rentals
    ADD COLUMN base_price_per_gpu DECIMAL(10,4) NOT NULL DEFAULT 0.0,
    ADD COLUMN gpu_count INT NOT NULL DEFAULT 1;

-- Add indexes for query performance
CREATE INDEX idx_rentals_base_price ON billing.rentals(base_price_per_gpu);
CREATE INDEX idx_rentals_gpu_count ON billing.rentals(gpu_count);

-- Add comments to document the fields
COMMENT ON COLUMN billing.rentals.base_price_per_gpu IS
    'Base price per GPU per hour from rental request (including markup)';
COMMENT ON COLUMN billing.rentals.gpu_count IS
    'Number of GPUs in this rental';

-- ============================================================================
-- PART 2: Remove package dependency from rentals
-- ============================================================================

-- Drop the package_id column entirely (no active rentals during deployment)
ALTER TABLE billing.rentals DROP COLUMN package_id;

-- ============================================================================
-- PART 3: Drop package infrastructure tables
-- ============================================================================

-- Drop tables with FK constraints to billing_packages first
DROP TABLE IF EXISTS billing.user_packages;
DROP TABLE IF EXISTS billing.package_gpu_rates;
DROP TABLE IF EXISTS billing.user_preferences;
DROP TABLE IF EXISTS billing.billing_rules;

-- Drop package_id column from billing_summary_facts (keep table for historical data)
ALTER TABLE billing.billing_summary_facts DROP COLUMN IF EXISTS package_id;

-- Drop main packages table
DROP TABLE IF EXISTS billing.billing_packages;

-- ============================================================================
-- PART 4: Drop dynamic pricing infrastructure
-- ============================================================================

-- Drop price cache and history tables
DROP TABLE IF EXISTS billing.price_cache;
DROP TABLE IF EXISTS billing.price_history;

-- ============================================================================
-- PART 5: Clean up defaults (optional - for existing data migration)
-- ============================================================================

-- After migration completes, you may want to remove defaults to enforce
-- explicit pricing on all new rentals:
-- ALTER TABLE billing.rentals
--     ALTER COLUMN base_price_per_gpu DROP DEFAULT,
--     ALTER COLUMN gpu_count DROP DEFAULT;

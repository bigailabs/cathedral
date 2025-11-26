-- Migration 029: Unify Rentals Tables
-- Merges community_rentals and secure_cloud_rentals into a single rentals table
-- with a cloud_type discriminator column
--
-- Changes:
-- 1. Add cloud_type column to community_rentals
-- 2. Move node_id/validator_id into metadata JSONB (they're pure metadata, never queried)
-- 3. Drop hourly_rate (derived from base_price_per_gpu * gpu_count)
-- 4. Migrate secure_cloud_rentals data
-- 5. Drop node_id/validator_id columns
-- 6. Rename community_rentals -> rentals
-- 7. Drop secure_cloud_rentals table
-- 8. Update indexes

-- ============================================================================
-- PART 1: Add cloud_type column to community_rentals
-- ============================================================================

ALTER TABLE billing.community_rentals
    ADD COLUMN cloud_type VARCHAR(20) NOT NULL DEFAULT 'community';

-- ============================================================================
-- PART 2: Move community-specific fields (node_id, validator_id) into metadata
-- ============================================================================

UPDATE billing.community_rentals
SET metadata = jsonb_build_object(
    'node_id', node_id,
    'validator_id', validator_id
) || COALESCE(metadata, '{}'::jsonb);

-- ============================================================================
-- PART 3: Drop hourly_rate column (derived at runtime from base_price_per_gpu * gpu_count)
-- ============================================================================

ALTER TABLE billing.community_rentals DROP COLUMN IF EXISTS hourly_rate;

-- ============================================================================
-- PART 4: Migrate secure_cloud_rentals data into community_rentals
-- ============================================================================

-- Use ON CONFLICT to skip any duplicate rental_ids (shouldn't happen in production,
-- but could occur in testing environments)
INSERT INTO billing.community_rentals (
    rental_id, user_id, node_id, validator_id, status,
    resource_spec, start_time, end_time, total_cost,
    metadata, created_at, updated_at, base_price_per_gpu, gpu_count, cloud_type
)
SELECT
    rental_id, user_id,
    '',  -- node_id placeholder (will be dropped)
    '',  -- validator_id placeholder (will be dropped)
    status,
    resource_spec,
    start_time, end_time, total_cost,
    jsonb_build_object(
        'provider', provider,
        'provider_instance_id', provider_instance_id,
        'offering_id', offering_id
    ) || COALESCE(metadata, '{}'::jsonb) as metadata,
    created_at, updated_at, base_price_per_gpu, gpu_count,
    'secure' as cloud_type
FROM billing.secure_cloud_rentals
ON CONFLICT (rental_id) DO NOTHING;

-- ============================================================================
-- PART 5: Drop node_id and validator_id columns (now in metadata)
-- ============================================================================

ALTER TABLE billing.community_rentals DROP COLUMN node_id;
ALTER TABLE billing.community_rentals DROP COLUMN validator_id;

-- ============================================================================
-- PART 6: Rename to unified rentals table
-- ============================================================================

ALTER TABLE billing.community_rentals RENAME TO rentals;

-- ============================================================================
-- PART 7: Drop secure_cloud_rentals table
-- ============================================================================

DROP TABLE billing.secure_cloud_rentals;

-- ============================================================================
-- PART 8: Update indexes
-- ============================================================================

-- Drop node_id index (no longer exists)
DROP INDEX IF EXISTS billing.idx_community_rentals_node_id;

-- Rename community_rentals indexes to rentals
ALTER INDEX IF EXISTS billing.idx_community_rentals_user_id RENAME TO idx_rentals_user_id;
ALTER INDEX IF EXISTS billing.idx_community_rentals_status RENAME TO idx_rentals_status;
ALTER INDEX IF EXISTS billing.idx_community_rentals_start_time RENAME TO idx_rentals_start_time;
ALTER INDEX IF EXISTS billing.idx_community_rentals_end_time RENAME TO idx_rentals_end_time;
ALTER INDEX IF EXISTS billing.idx_community_rentals_active RENAME TO idx_rentals_active;
ALTER INDEX IF EXISTS billing.idx_community_rentals_base_price RENAME TO idx_rentals_base_price;
ALTER INDEX IF EXISTS billing.idx_community_rentals_gpu_count RENAME TO idx_rentals_gpu_count;

-- Add index on cloud_type for filtering
CREATE INDEX idx_rentals_cloud_type ON billing.rentals(cloud_type);

-- ============================================================================
-- PART 9: Update table comment
-- ============================================================================

COMMENT ON TABLE billing.rentals IS
    'Unified GPU rental records for both community cloud and secure cloud rentals';

COMMENT ON COLUMN billing.rentals.cloud_type IS
    'Type of cloud: community (validator-based) or secure (direct provider API)';

COMMENT ON COLUMN billing.rentals.metadata IS
    'Type-specific metadata: community has node_id/validator_id, secure has provider/provider_instance_id/offering_id';

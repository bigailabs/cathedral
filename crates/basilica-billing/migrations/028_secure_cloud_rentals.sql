-- Migration 028: Secure Cloud Rentals
-- This migration splits rentals table into community and secure cloud variants
--
-- Changes:
-- 1. Rename existing rentals table to community_rentals
-- 2. Create new secure_cloud_rentals table for direct provider rentals
-- 3. Update related indexes
-- 4. Both tables remain compatible with usage_events and telemetry_buffer via rental_id

-- ============================================================================
-- PART 1: Rename existing rentals table to community_rentals
-- ============================================================================

ALTER TABLE billing.rentals RENAME TO community_rentals;

-- Rename all indexes to reflect new table name
ALTER INDEX IF EXISTS idx_rentals_user_id RENAME TO idx_community_rentals_user_id;
ALTER INDEX IF EXISTS idx_rentals_node_id RENAME TO idx_community_rentals_node_id;
ALTER INDEX IF EXISTS idx_rentals_status RENAME TO idx_community_rentals_status;
ALTER INDEX IF EXISTS idx_rentals_start_time RENAME TO idx_community_rentals_start_time;
ALTER INDEX IF EXISTS idx_rentals_end_time RENAME TO idx_community_rentals_end_time;
ALTER INDEX IF EXISTS idx_rentals_active RENAME TO idx_community_rentals_active;
ALTER INDEX IF EXISTS idx_rentals_base_price RENAME TO idx_community_rentals_base_price;
ALTER INDEX IF EXISTS idx_rentals_gpu_count RENAME TO idx_community_rentals_gpu_count;

-- Update table comment
COMMENT ON TABLE billing.community_rentals IS
    'Stores GPU rental records for community cloud (validator-based) rentals';

-- ============================================================================
-- PART 2: Create secure_cloud_rentals table
-- ============================================================================

CREATE TABLE billing.secure_cloud_rentals (
    rental_id UUID PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    provider VARCHAR(50) NOT NULL,              -- Provider name (datacrunch, hyperstack, etc.)
    provider_instance_id VARCHAR(255) NOT NULL, -- Provider's instance ID
    offering_id VARCHAR(255) NOT NULL,          -- Original offering ID from aggregator
    resource_spec JSONB NOT NULL DEFAULT '{}',  -- GPU specs, CPU, memory, etc.
    base_price_per_gpu DECIMAL(10,4) NOT NULL,  -- Base price per GPU per hour (including markup)
    gpu_count INT NOT NULL,                      -- Number of GPUs in this rental
    start_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    end_time TIMESTAMPTZ,
    max_duration_hours INTEGER NOT NULL DEFAULT 24,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    total_cost DECIMAL(10, 2),
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- PART 3: Create indexes for secure_cloud_rentals
-- ============================================================================

CREATE INDEX idx_secure_cloud_rentals_user_id ON billing.secure_cloud_rentals(user_id);
CREATE INDEX idx_secure_cloud_rentals_provider ON billing.secure_cloud_rentals(provider);
CREATE INDEX idx_secure_cloud_rentals_provider_instance ON billing.secure_cloud_rentals(provider_instance_id);
CREATE INDEX idx_secure_cloud_rentals_status ON billing.secure_cloud_rentals(status);
CREATE INDEX idx_secure_cloud_rentals_start_time ON billing.secure_cloud_rentals(start_time);
CREATE INDEX idx_secure_cloud_rentals_end_time ON billing.secure_cloud_rentals(end_time);
CREATE INDEX idx_secure_cloud_rentals_active ON billing.secure_cloud_rentals(user_id, status)
    WHERE status IN ('active', 'pending');
CREATE INDEX idx_secure_cloud_rentals_base_price ON billing.secure_cloud_rentals(base_price_per_gpu);
CREATE INDEX idx_secure_cloud_rentals_gpu_count ON billing.secure_cloud_rentals(gpu_count);

-- ============================================================================
-- PART 4: Add comments to document the new table
-- ============================================================================

COMMENT ON TABLE billing.secure_cloud_rentals IS
    'Stores GPU rental records for secure cloud (direct provider API) rentals';

COMMENT ON COLUMN billing.secure_cloud_rentals.rental_id IS
    'Unique identifier for the rental';
COMMENT ON COLUMN billing.secure_cloud_rentals.user_id IS
    'User who initiated the rental';
COMMENT ON COLUMN billing.secure_cloud_rentals.provider IS
    'Cloud provider name (e.g., datacrunch, hyperstack, lambda, hydrahost)';
COMMENT ON COLUMN billing.secure_cloud_rentals.provider_instance_id IS
    'Provider''s instance ID for this deployment';
COMMENT ON COLUMN billing.secure_cloud_rentals.offering_id IS
    'Original offering ID from aggregator service';
COMMENT ON COLUMN billing.secure_cloud_rentals.resource_spec IS
    'JSON specification of resources being rented';
COMMENT ON COLUMN billing.secure_cloud_rentals.base_price_per_gpu IS
    'Base price per GPU per hour from provider (including markup)';
COMMENT ON COLUMN billing.secure_cloud_rentals.gpu_count IS
    'Number of GPUs in this rental';
COMMENT ON COLUMN billing.secure_cloud_rentals.status IS
    'Current status: pending, active, completed, cancelled, failed';
COMMENT ON COLUMN billing.secure_cloud_rentals.total_cost IS
    'Final cost of the rental once completed';
COMMENT ON COLUMN billing.secure_cloud_rentals.metadata IS
    'Additional rental metadata as JSON';

-- ============================================================================
-- NOTES
-- ============================================================================

-- Both community_rentals and secure_cloud_rentals share the same rental_id space (UUID)
-- This allows unified querying of usage_events and telemetry_buffer tables which
-- reference rental_id without needing to know the cloud type.
--
-- To query all rentals across both types, use application-level merging:
--   SELECT * FROM billing.community_rentals UNION ALL SELECT * FROM billing.secure_cloud_rentals
--
-- The separate tables provide type safety and avoid nullable fields that would only
-- apply to one cloud type (e.g., validator_id for community, provider for secure cloud).

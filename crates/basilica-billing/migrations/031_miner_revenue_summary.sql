-- This migration creates infrastructure for tracking and reporting miner revenue
-- from rental activity for accounting/finance purposes

-- ===========================================================================
-- 1. Create miner_revenue_summary table
-- ===========================================================================

CREATE TABLE billing.miner_revenue_summary (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id VARCHAR(128) NOT NULL,
    validator_id VARCHAR(128),  -- Nullable, preserves NULL from source
    miner_uid INTEGER,          -- Bittensor miner UID for payment reconciliation (nullable)

    -- Time period this summary covers
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,

    -- Aggregated metrics for this period
    total_rentals INTEGER NOT NULL DEFAULT 0,
    completed_rentals INTEGER NOT NULL DEFAULT 0,
    failed_rentals INTEGER NOT NULL DEFAULT 0,
    total_revenue DECIMAL(20,8) NOT NULL DEFAULT 0,
    total_hours DECIMAL(15,2) NOT NULL DEFAULT 0,

    -- Computed metrics
    avg_hourly_rate DECIMAL(10,4),
    avg_rental_duration_hours DECIMAL(10,2),

    -- Audit fields
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    computation_version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ===========================================================================
-- 2. Create indexes for efficient querying
-- ===========================================================================

-- Query by node_id to get all summaries for a specific miner
CREATE INDEX idx_miner_revenue_summary_node ON billing.miner_revenue_summary(node_id);

-- Query by validator_id to get all summaries for a specific validator
CREATE INDEX idx_miner_revenue_summary_validator ON billing.miner_revenue_summary(validator_id);

-- Query by time period to get all summaries for a date range
CREATE INDEX idx_miner_revenue_summary_period ON billing.miner_revenue_summary(period_start, period_end);

-- Query most recent computations
CREATE INDEX idx_miner_revenue_summary_computed_at ON billing.miner_revenue_summary(computed_at DESC);

-- Composite index for efficient lookups of specific miner/validator in a period
CREATE INDEX idx_miner_revenue_summary_lookup ON billing.miner_revenue_summary(
    node_id, validator_id, period_start, period_end, computed_at DESC
);

-- Query by miner_uid to get all summaries for a specific Bittensor miner
CREATE INDEX idx_miner_revenue_summary_miner_uid ON billing.miner_revenue_summary(miner_uid)
    WHERE miner_uid IS NOT NULL;

-- ===========================================================================
-- 3. Add table and column comments for documentation
-- ===========================================================================

COMMENT ON TABLE billing.miner_revenue_summary IS
    'Append-only summary table tracking miner revenue by time period. Each refresh creates new snapshot rows for audit trail.';

COMMENT ON COLUMN billing.miner_revenue_summary.node_id IS
    'The executor/miner node that earned the revenue';

COMMENT ON COLUMN billing.miner_revenue_summary.validator_id IS
    'The validator that facilitated the rental (nullable)';

COMMENT ON COLUMN billing.miner_revenue_summary.miner_uid IS
    'Bittensor miner UID for payment reconciliation (nullable, NULL means not recorded)';

COMMENT ON COLUMN billing.miner_revenue_summary.period_start IS
    'Start of the time period (inclusive)';

COMMENT ON COLUMN billing.miner_revenue_summary.period_end IS
    'End of the time period (exclusive)';

COMMENT ON COLUMN billing.miner_revenue_summary.total_rentals IS
    'Total number of rentals in this period (all statuses)';

COMMENT ON COLUMN billing.miner_revenue_summary.completed_rentals IS
    'Number of rentals with status = completed';

COMMENT ON COLUMN billing.miner_revenue_summary.failed_rentals IS
    'Number of rentals with status = failed';

COMMENT ON COLUMN billing.miner_revenue_summary.total_revenue IS
    'Sum of total_cost from all rentals in this period (TAO)';

COMMENT ON COLUMN billing.miner_revenue_summary.total_hours IS
    'Total rental hours (sum of end_time - start_time)';

COMMENT ON COLUMN billing.miner_revenue_summary.computed_at IS
    'When this summary snapshot was computed (for temporal tracking)';

COMMENT ON COLUMN billing.miner_revenue_summary.computation_version IS
    'Version of the computation logic (for migration tracking)';

-- ===========================================================================
-- 4. Create stored function for refreshing summaries
-- ===========================================================================

-- Updated for unified rentals table (migration 029):
-- - node_id, validator_id, and miner_uid are now in metadata JSONB field
-- - hourly_rate column removed, calculate from base_price_per_gpu * gpu_count
-- - Only include community cloud rentals (secure cloud handled internally)

CREATE OR REPLACE FUNCTION billing.refresh_miner_revenue_summary(
    p_period_start TIMESTAMPTZ,
    p_period_end TIMESTAMPTZ,
    p_computation_version INTEGER DEFAULT 1
)
RETURNS INTEGER AS $$
DECLARE
    rows_inserted INTEGER;
BEGIN
    -- Insert new summary rows (append-only, never update existing)
    INSERT INTO billing.miner_revenue_summary (
        node_id,
        validator_id,
        miner_uid,
        period_start,
        period_end,
        total_rentals,
        completed_rentals,
        failed_rentals,
        total_revenue,
        total_hours,
        avg_hourly_rate,
        avg_rental_duration_hours,
        computation_version
    )
    SELECT
        r.metadata->>'node_id' AS node_id,
        r.metadata->>'validator_id' AS validator_id,
        -- Extract miner_uid from metadata, cast to integer (NULL if not present or invalid)
        (r.metadata->>'miner_uid')::INTEGER AS miner_uid,
        p_period_start,
        p_period_end,
        COUNT(*) AS total_rentals,
        COUNT(*) FILTER (WHERE r.status = 'completed') AS completed_rentals,
        COUNT(*) FILTER (WHERE r.status = 'failed') AS failed_rentals,
        COALESCE(SUM(r.total_cost), 0) AS total_revenue,
        COALESCE(
            SUM(EXTRACT(EPOCH FROM (r.end_time - r.start_time)) / 3600),
            0
        ) AS total_hours,
        AVG(r.base_price_per_gpu * r.gpu_count) AS avg_hourly_rate,
        AVG(EXTRACT(EPOCH FROM (r.end_time - r.start_time)) / 3600) AS avg_rental_duration_hours,
        p_computation_version
    FROM billing.rentals r
    WHERE r.cloud_type = 'community'  -- Only community rentals (miners we need to pay)
      AND r.end_time IS NOT NULL
      AND r.end_time >= p_period_start
      AND r.end_time < p_period_end
    GROUP BY r.metadata->>'node_id', r.metadata->>'validator_id', (r.metadata->>'miner_uid')::INTEGER;

    GET DIAGNOSTICS rows_inserted = ROW_COUNT;
    RETURN rows_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION billing.refresh_miner_revenue_summary IS
    'Refresh miner revenue summary for a given time period. Creates new snapshot rows (append-only). Only includes community cloud rentals with end_time IS NOT NULL.';

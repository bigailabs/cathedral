-- Migration 021: Dynamic pricing support
-- This migration adds tables and columns for dynamic GPU pricing

-- ===========================================================================
-- 1. Create price_cache table
-- ===========================================================================
CREATE TABLE IF NOT EXISTS billing.price_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gpu_model VARCHAR(100) NOT NULL,
    vram_gb INT,

    -- Pricing information
    market_price_per_hour DECIMAL(10, 4) NOT NULL CHECK (market_price_per_hour >= 0),
    discounted_price_per_hour DECIMAL(10, 4) NOT NULL CHECK (discounted_price_per_hour >= 0),
    discount_percent DECIMAL(5, 2) NOT NULL,

    -- Source tracking
    source VARCHAR(50) NOT NULL,  -- 'aws', 'azure', 'aggregated', etc.
    provider VARCHAR(50) NOT NULL,
    location VARCHAR(100),
    instance_name VARCHAR(100),
    is_spot BOOLEAN DEFAULT false,

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,

    -- Unique constraint: one price per (gpu_model, provider, location, spot type)
    UNIQUE(gpu_model, provider, location, is_spot)
);

-- Indexes for price_cache
CREATE INDEX IF NOT EXISTS idx_price_cache_gpu_model
    ON billing.price_cache(gpu_model);

CREATE INDEX IF NOT EXISTS idx_price_cache_expires_at
    ON billing.price_cache(expires_at);

CREATE INDEX IF NOT EXISTS idx_price_cache_updated_at
    ON billing.price_cache(updated_at DESC);

-- Table and column comments
COMMENT ON TABLE billing.price_cache IS
    'Stores cached GPU prices from external sources with expiration';

COMMENT ON COLUMN billing.price_cache.gpu_model IS
    'GPU model name (e.g., H100, A100, H200)';

COMMENT ON COLUMN billing.price_cache.market_price_per_hour IS
    'Raw market price per hour before discount';

COMMENT ON COLUMN billing.price_cache.discounted_price_per_hour IS
    'Final price per hour after applying discount';

COMMENT ON COLUMN billing.price_cache.discount_percent IS
    'Discount percentage applied (negative = discount, positive = markup)';

COMMENT ON COLUMN billing.price_cache.source IS
    'Source of aggregation (e.g., aws, azure, aggregated)';

COMMENT ON COLUMN billing.price_cache.provider IS
    'Cloud provider (e.g., aws, azure, gcp, vastai)';

COMMENT ON COLUMN billing.price_cache.expires_at IS
    'When this cache entry expires and should be refreshed';


-- ===========================================================================
-- 2. Create price_history table
-- ===========================================================================
CREATE TABLE IF NOT EXISTS billing.price_history (
    id BIGSERIAL PRIMARY KEY,
    gpu_model VARCHAR(100) NOT NULL,
    price_per_hour DECIMAL(10, 4) NOT NULL,
    source VARCHAR(50) NOT NULL,
    provider VARCHAR(50) NOT NULL,
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for price_history
CREATE INDEX IF NOT EXISTS idx_price_history_gpu_model
    ON billing.price_history(gpu_model);

CREATE INDEX IF NOT EXISTS idx_price_history_recorded_at
    ON billing.price_history(recorded_at DESC);

COMMENT ON TABLE billing.price_history IS
    'Historical record of GPU price changes over time';

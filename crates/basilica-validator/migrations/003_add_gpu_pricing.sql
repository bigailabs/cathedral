-- Add pricing information to nodes
-- Allows tracking hourly rental rates per node

ALTER TABLE miner_nodes ADD COLUMN hourly_rate_cents INTEGER DEFAULT NULL;

-- Index for efficient pricing lookups
CREATE INDEX IF NOT EXISTS idx_miner_nodes_pricing ON miner_nodes(node_id, hourly_rate_cents);

-- Comment: hourly_rate_cents stores the price in cents per GPU per hour (e.g., 250 = $2.50/hour per GPU)
-- NULL values indicate pricing not yet configured or retrieved from miner

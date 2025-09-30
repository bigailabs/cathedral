-- Add gpu_uuids column to miner_nodes
-- This stores a JSON array of GPU UUIDs for each node
ALTER TABLE
  miner_nodes
ADD
  COLUMN gpu_uuids TEXT;

-- Add index for GPU UUIDs lookups
CREATE INDEX IF NOT EXISTS idx_nodes_gpu_uuids ON miner_nodes(gpu_uuids);

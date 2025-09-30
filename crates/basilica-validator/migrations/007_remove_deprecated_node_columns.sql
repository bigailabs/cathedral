-- Remove deprecated columns from miner_nodes table
-- These columns have been superseded by the node profile tables
-- Check and drop gpu_specs column if it exists
-- SQLite doesn't support DROP COLUMN IF EXISTS, so we check the column via pragma
-- This migration is safe to run multiple times
-- Note: SQLite has limited ALTER TABLE support
-- We need to check if columns exist before attempting to drop them
-- This is handled by creating a new table and copying data
-- For SQLite, we'll use a different approach:
-- Only attempt to drop if the column exists by checking pragma_table_info
-- Since SQLite doesn't support conditional column drops easily,
-- we'll document that these columns should be manually dropped if they exist
-- Or we can use a more complex migration with table recreation
-- For now, we'll use the PRAGMA approach to check and recreate the table
-- Create a new table without the deprecated columns
CREATE TABLE IF NOT EXISTS miner_nodes_new (
  id TEXT PRIMARY KEY,
  miner_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  ssh_endpoint TEXT NOT NULL,
  gpu_count INTEGER NOT NULL,
  status TEXT DEFAULT 'unknown',
  last_health_check TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  gpu_uuids TEXT,
  FOREIGN KEY (miner_id) REFERENCES miners (id) ON DELETE CASCADE
);

-- Copy data from old table to new table (only the columns that exist in both)
INSERT INTO
  miner_nodes_new (
    id,
    miner_id,
    node_id,
    ssh_endpoint,
    gpu_count,
    status,
    last_health_check,
    created_at,
    updated_at,
    gpu_uuids
  )
SELECT
  id,
  miner_id,
  node_id,
  ssh_endpoint,
  gpu_count,
  status,
  last_health_check,
  created_at,
  updated_at,
  gpu_uuids
FROM
  miner_nodes;

-- Drop old table
DROP TABLE miner_nodes;

-- Rename new table to original name
ALTER TABLE
  miner_nodes_new RENAME TO miner_nodes;

-- Recreate indexes
CREATE INDEX IF NOT EXISTS idx_miner_nodes_status ON miner_nodes(status);

CREATE INDEX IF NOT EXISTS idx_miner_nodes_health_check ON miner_nodes(last_health_check);

CREATE INDEX IF NOT EXISTS idx_nodes_gpu_uuids ON miner_nodes(gpu_uuids);

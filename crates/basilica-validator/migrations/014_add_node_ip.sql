-- Clear existing rows to ensure clean state for new uniqueness constraints
DELETE FROM miner_nodes;

-- Add node_ip column for uniqueness enforcement (one node per physical host)
ALTER TABLE miner_nodes ADD COLUMN node_ip TEXT NOT NULL DEFAULT '';

-- Unique index: only one node per IP across all miners
CREATE UNIQUE INDEX idx_miner_nodes_node_ip ON miner_nodes(node_ip) WHERE node_ip != '';

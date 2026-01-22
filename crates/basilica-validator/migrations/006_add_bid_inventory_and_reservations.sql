CREATE TABLE IF NOT EXISTS miner_bid_nodes (
    bid_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    miner_id TEXT NOT NULL,
    gpu_category TEXT NOT NULL,
    gpu_count INTEGER NOT NULL,
    snapshot_at TEXT NOT NULL,
    PRIMARY KEY (bid_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_bid_nodes_bid ON miner_bid_nodes(bid_id);
CREATE INDEX IF NOT EXISTS idx_bid_nodes_node ON miner_bid_nodes(node_id);
CREATE INDEX IF NOT EXISTS idx_bid_nodes_miner ON miner_bid_nodes(miner_id);

CREATE TABLE IF NOT EXISTS node_reservations (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    miner_id TEXT NOT NULL,
    rental_id TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_node_reservations_node ON node_reservations(node_id);
CREATE INDEX IF NOT EXISTS idx_node_reservations_expires ON node_reservations(expires_at);



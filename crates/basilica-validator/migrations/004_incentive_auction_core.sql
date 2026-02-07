-- Incentive mechanism core schema (auction, bids, reservations)
-- Consolidated from prior pre-deploy migrations.

-- Auction epoch tracking
CREATE TABLE IF NOT EXISTS auction_epochs (
    id TEXT PRIMARY KEY,
    start_block INTEGER NOT NULL,
    end_block INTEGER,
    baseline_prices_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Miner bids per epoch (prices stored in cents)
CREATE TABLE IF NOT EXISTS miner_bids (
    id TEXT PRIMARY KEY,
    miner_hotkey TEXT NOT NULL,
    miner_uid INTEGER NOT NULL,
    gpu_category TEXT NOT NULL,
    bid_per_hour_cents INTEGER NOT NULL,
    gpu_count INTEGER NOT NULL,
    attestation BLOB,
    signature BLOB NOT NULL,
    nonce TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    epoch_id TEXT NOT NULL,
    is_valid INTEGER DEFAULT 1
);

-- Clearing results (prices stored in cents)
CREATE TABLE IF NOT EXISTS auction_clearing_results (
    id TEXT PRIMARY KEY,
    epoch_id TEXT NOT NULL,
    gpu_category TEXT NOT NULL,
    baseline_price_cents INTEGER NOT NULL,
    clearing_price_cents INTEGER,
    total_capacity INTEGER NOT NULL,
    winners_count INTEGER NOT NULL,
    cleared_at TEXT NOT NULL,
    FOREIGN KEY (epoch_id) REFERENCES auction_epochs(id)
);

-- Node inventory snapshot tied to a bid
CREATE TABLE IF NOT EXISTS miner_bid_nodes (
    bid_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    miner_id TEXT NOT NULL,
    gpu_category TEXT NOT NULL,
    gpu_count INTEGER NOT NULL,
    snapshot_at TEXT NOT NULL,
    PRIMARY KEY (bid_id, node_id)
);

-- Short-lived node reservations during rental creation
CREATE TABLE IF NOT EXISTS node_reservations (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    miner_id TEXT NOT NULL,
    rental_id TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Bid and auction indexes
CREATE UNIQUE INDEX IF NOT EXISTS idx_miner_bids_unique_hotkey_category_epoch
    ON miner_bids(miner_hotkey, gpu_category, epoch_id);
CREATE INDEX IF NOT EXISTS idx_miner_bids_epoch ON miner_bids(epoch_id);
CREATE INDEX IF NOT EXISTS idx_miner_bids_category ON miner_bids(gpu_category);
CREATE INDEX IF NOT EXISTS idx_miner_bids_hotkey_nonce ON miner_bids(miner_hotkey, nonce, submitted_at);
CREATE INDEX IF NOT EXISTS idx_miner_bids_valid ON miner_bids(epoch_id, gpu_category, is_valid, submitted_at);
CREATE INDEX IF NOT EXISTS idx_miner_bids_price ON miner_bids(epoch_id, gpu_category, is_valid, bid_per_hour_cents, submitted_at);
CREATE INDEX IF NOT EXISTS idx_clearing_results_epoch ON auction_clearing_results(epoch_id);

-- Bid inventory and reservation indexes
CREATE INDEX IF NOT EXISTS idx_bid_nodes_bid ON miner_bid_nodes(bid_id);
CREATE INDEX IF NOT EXISTS idx_bid_nodes_node ON miner_bid_nodes(node_id);
CREATE INDEX IF NOT EXISTS idx_bid_nodes_miner ON miner_bid_nodes(miner_id);
CREATE INDEX IF NOT EXISTS idx_node_reservations_node ON node_reservations(node_id);
CREATE INDEX IF NOT EXISTS idx_node_reservations_expires ON node_reservations(expires_at);

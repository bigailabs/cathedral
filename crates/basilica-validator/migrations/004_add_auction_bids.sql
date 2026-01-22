-- Auction epoch tracking
CREATE TABLE IF NOT EXISTS auction_epochs (
    id TEXT PRIMARY KEY,
    start_block INTEGER NOT NULL,
    end_block INTEGER,
    baseline_prices_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Miner bids per epoch
CREATE TABLE IF NOT EXISTS miner_bids (
    id TEXT PRIMARY KEY,
    miner_hotkey TEXT NOT NULL,
    miner_uid INTEGER NOT NULL,
    gpu_category TEXT NOT NULL,
    bid_per_hour REAL NOT NULL,
    gpu_count INTEGER NOT NULL,
    attestation BLOB,
    signature BLOB NOT NULL,
    submitted_at TEXT NOT NULL,
    epoch_id TEXT NOT NULL,
    is_valid INTEGER DEFAULT 1,
    UNIQUE(miner_hotkey, gpu_category, epoch_id)
);

-- Clearing results
CREATE TABLE IF NOT EXISTS auction_clearing_results (
    id TEXT PRIMARY KEY,
    epoch_id TEXT NOT NULL,
    gpu_category TEXT NOT NULL,
    baseline_price REAL NOT NULL,
    clearing_price REAL,
    total_capacity INTEGER NOT NULL,
    winners_count INTEGER NOT NULL,
    cleared_at TEXT NOT NULL,
    FOREIGN KEY (epoch_id) REFERENCES auction_epochs(id)
);

CREATE INDEX IF NOT EXISTS idx_miner_bids_epoch ON miner_bids(epoch_id);
CREATE INDEX IF NOT EXISTS idx_miner_bids_category ON miner_bids(gpu_category);
CREATE INDEX IF NOT EXISTS idx_clearing_results_epoch ON auction_clearing_results(epoch_id);



-- Convert bid prices from dollars (REAL) to cents (INTEGER)
-- This is a breaking change - existing bids will be converted

-- Step 1: Recreate miner_bids with INTEGER column for cents
CREATE TABLE miner_bids_new (
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

-- Copy data, converting dollars to cents (multiply by 100 and round)
INSERT INTO miner_bids_new (id, miner_hotkey, miner_uid, gpu_category, bid_per_hour_cents, gpu_count, attestation, signature, nonce, submitted_at, epoch_id, is_valid)
SELECT id, miner_hotkey, miner_uid, gpu_category,
       CAST(ROUND(bid_per_hour * 100) AS INTEGER),
       gpu_count, attestation, signature,
       COALESCE(nonce, ''),
       submitted_at, epoch_id, is_valid
FROM miner_bids;

-- Drop old table and rename new one
DROP TABLE miner_bids;
ALTER TABLE miner_bids_new RENAME TO miner_bids;

-- Recreate indexes
CREATE INDEX IF NOT EXISTS idx_miner_bids_epoch ON miner_bids(epoch_id);
CREATE INDEX IF NOT EXISTS idx_miner_bids_category ON miner_bids(gpu_category);
CREATE INDEX IF NOT EXISTS idx_miner_bids_hotkey_nonce ON miner_bids(miner_hotkey, nonce, submitted_at);
CREATE INDEX IF NOT EXISTS idx_miner_bids_valid ON miner_bids(epoch_id, gpu_category, is_valid, submitted_at);
CREATE INDEX IF NOT EXISTS idx_miner_bids_price ON miner_bids(epoch_id, gpu_category, is_valid, bid_per_hour_cents, submitted_at);

-- Step 2: Recreate auction_clearing_results with INTEGER columns for cents
CREATE TABLE auction_clearing_results_new (
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

-- Copy data, converting dollars to cents
INSERT INTO auction_clearing_results_new (id, epoch_id, gpu_category, baseline_price_cents, clearing_price_cents, total_capacity, winners_count, cleared_at)
SELECT id, epoch_id, gpu_category,
       CAST(ROUND(baseline_price * 100) AS INTEGER),
       CASE WHEN clearing_price IS NULL THEN NULL ELSE CAST(ROUND(clearing_price * 100) AS INTEGER) END,
       total_capacity, winners_count, cleared_at
FROM auction_clearing_results;

-- Drop old table and rename new one
DROP TABLE auction_clearing_results;
ALTER TABLE auction_clearing_results_new RENAME TO auction_clearing_results;

-- Recreate index
CREATE INDEX IF NOT EXISTS idx_clearing_results_epoch ON auction_clearing_results(epoch_id);

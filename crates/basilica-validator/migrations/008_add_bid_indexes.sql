CREATE INDEX IF NOT EXISTS idx_miner_bids_category_price
    ON miner_bids(gpu_category, bid_per_hour, is_valid, submitted_at);

CREATE INDEX IF NOT EXISTS idx_miner_bids_nonce_check
    ON miner_bids(miner_hotkey, nonce, submitted_at);


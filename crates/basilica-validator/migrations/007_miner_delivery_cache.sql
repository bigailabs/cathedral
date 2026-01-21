CREATE TABLE IF NOT EXISTS miner_delivery_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_hotkey TEXT NOT NULL,
    miner_uid INTEGER NOT NULL,
    gpu_category TEXT NOT NULL,
    period_start INTEGER NOT NULL,
    period_end INTEGER NOT NULL,
    total_hours REAL NOT NULL,
    user_revenue_usd REAL NOT NULL,
    miner_payment_usd REAL NOT NULL,
    received_at INTEGER NOT NULL,
    UNIQUE(miner_hotkey, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS idx_delivery_cache_period
    ON miner_delivery_cache(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_delivery_cache_hotkey
    ON miner_delivery_cache(miner_hotkey);


ALTER TABLE miner_delivery_cache ADD COLUMN node_id TEXT DEFAULT '';
ALTER TABLE miner_delivery_cache ADD COLUMN has_collateral INTEGER NOT NULL DEFAULT 0;
ALTER TABLE miner_delivery_cache ADD COLUMN payout_type TEXT NOT NULL DEFAULT '';
ALTER TABLE miner_delivery_cache ADD COLUMN cliff_days_remaining INTEGER NOT NULL DEFAULT 0;
ALTER TABLE miner_delivery_cache ADD COLUMN pending_alpha REAL NOT NULL DEFAULT 0;


ALTER TABLE miner_bids ADD COLUMN nonce TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_miner_bids_nonce ON miner_bids(nonce);



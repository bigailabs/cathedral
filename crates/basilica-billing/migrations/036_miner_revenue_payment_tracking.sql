-- Add payment tracking columns to miner_revenue_summary
--
-- Tracks whether each revenue summary record has been paid to the miner,
-- and stores the blockchain transaction hash for audit trail.

ALTER TABLE billing.miner_revenue_summary
    ADD COLUMN paid BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN tx_hash VARCHAR(128);

-- Index for efficiently querying unpaid records
CREATE INDEX idx_miner_revenue_summary_unpaid
    ON billing.miner_revenue_summary(paid)
    WHERE paid = FALSE;

-- Index for looking up payments by transaction hash
CREATE INDEX idx_miner_revenue_summary_tx_hash
    ON billing.miner_revenue_summary(tx_hash)
    WHERE tx_hash IS NOT NULL;

COMMENT ON COLUMN billing.miner_revenue_summary.paid IS
    'Whether this revenue summary has been paid to the miner';
COMMENT ON COLUMN billing.miner_revenue_summary.tx_hash IS
    'Blockchain transaction hash for the payment (TAO or Alpha transfer)';

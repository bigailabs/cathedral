-- Reconciliation sweeps for hotwallet -> coldwallet transfers
-- Tracks all sweep attempts with complete audit trail

CREATE TABLE IF NOT EXISTS reconciliation_sweeps (
  id                        BIGSERIAL PRIMARY KEY,
  account_hex               TEXT NOT NULL,
  hotwallet_address_ss58    TEXT NOT NULL,
  coldwallet_address_ss58   TEXT NOT NULL,
  balance_before_plancks    NUMERIC(78,0) NOT NULL,
  sweep_amount_plancks      NUMERIC(78,0) NOT NULL,
  estimated_fee_plancks     NUMERIC(78,0) NOT NULL,
  balance_after_plancks     NUMERIC(78,0),
  status                    TEXT NOT NULL DEFAULT 'pending',
  dry_run                   BOOLEAN NOT NULL DEFAULT TRUE,
  tx_hash                   TEXT,
  block_number              BIGINT,
  error_message             TEXT,
  initiated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at              TIMESTAMPTZ,

  CONSTRAINT fk_account FOREIGN KEY (account_hex)
    REFERENCES deposit_accounts(account_id_hex),
  CONSTRAINT valid_status CHECK (status IN ('pending', 'submitted', 'confirmed', 'failed')),
  CONSTRAINT valid_sweep_amount CHECK (sweep_amount_plancks > 0),
  CONSTRAINT valid_balance CHECK (balance_before_plancks >= 0)
);

CREATE INDEX IF NOT EXISTS idx_sweeps_account ON reconciliation_sweeps(account_hex);
CREATE INDEX IF NOT EXISTS idx_sweeps_status ON reconciliation_sweeps(status, initiated_at);
CREATE INDEX IF NOT EXISTS idx_sweeps_tx_hash ON reconciliation_sweeps(tx_hash) WHERE tx_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sweeps_initiated ON reconciliation_sweeps(initiated_at DESC);

COMMENT ON TABLE reconciliation_sweeps IS 'Audit log of hotwallet to coldwallet reconciliation sweeps';
COMMENT ON COLUMN reconciliation_sweeps.dry_run IS 'If true, sweep was simulated but not executed on-chain';
COMMENT ON COLUMN reconciliation_sweeps.status IS 'pending: created | submitted: tx sent | confirmed: finalized | failed: error occurred';

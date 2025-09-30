-- Add gpu_uuid column to miner_prover_results
-- This enables tracking of individual GPU attestation results by UUID
ALTER TABLE
  miner_prover_results
ADD
  COLUMN gpu_uuid TEXT;

-- Add index for GPU UUID lookups
CREATE INDEX IF NOT EXISTS idx_prover_results_gpu_uuid ON miner_prover_results(gpu_uuid);

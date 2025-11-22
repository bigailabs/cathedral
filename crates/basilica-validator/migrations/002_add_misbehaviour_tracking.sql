-- Add misbehaviour tracking table for ban system
CREATE TABLE IF NOT EXISTS executor_misbehaviour_log (
    miner_uid INTEGER NOT NULL,
    executor_id TEXT NOT NULL,
    gpu_uuid TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    endpoint_executor TEXT NOT NULL,
    type_of_misbehaviour TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (miner_uid, executor_id, recorded_at)
);

-- Create index for efficient queries
CREATE INDEX IF NOT EXISTS idx_misbehaviour_miner_executor
    ON executor_misbehaviour_log(miner_uid, executor_id);

CREATE INDEX IF NOT EXISTS idx_misbehaviour_recorded_at
    ON executor_misbehaviour_log(recorded_at);

CREATE INDEX IF NOT EXISTS idx_misbehaviour_gpu_uuid
    ON executor_misbehaviour_log(gpu_uuid);

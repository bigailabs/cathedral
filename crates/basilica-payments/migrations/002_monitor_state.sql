-- Monitor state table for tracking blockchain scan progress
-- This ensures no blocks are skipped and allows recovery after restarts

CREATE TABLE IF NOT EXISTS monitor_state (
  monitor_id         TEXT PRIMARY KEY,
  last_scanned_block BIGINT NOT NULL,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Initialize with a safe default (will be updated on first run)
INSERT INTO monitor_state (monitor_id, last_scanned_block)
VALUES ('payments_monitor', 0)
ON CONFLICT (monitor_id) DO NOTHING;

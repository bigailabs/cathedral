CREATE TABLE IF NOT EXISTS availability_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  miner_uid INTEGER NOT NULL,
  hotkey TEXT NOT NULL,
  node_id TEXT NOT NULL,
  is_available INTEGER NOT NULL CHECK (is_available IN (0, 1)),
  is_rented INTEGER NOT NULL CHECK (is_rented IN (0, 1)),
  is_validated INTEGER NOT NULL CHECK (is_validated IN (0, 1)),
  source TEXT NOT NULL,
  source_metadata TEXT,
  row_effective_at INTEGER NOT NULL,
  row_expiration_at INTEGER,
  is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (is_current = 1 AND row_expiration_at IS NULL)
    OR (is_current = 0 AND row_expiration_at IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_availability_log_current_unique
  ON availability_log (hotkey, node_id)
  WHERE is_current = 1;

CREATE INDEX IF NOT EXISTS idx_availability_log_hotkey_node_effective
  ON availability_log (hotkey, node_id, row_effective_at DESC);

CREATE INDEX IF NOT EXISTS idx_availability_log_expiration
  ON availability_log (row_expiration_at);

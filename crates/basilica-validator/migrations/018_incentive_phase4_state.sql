CREATE TABLE IF NOT EXISTS incentive_cu_generator_progress (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_completed_hour_end_ms INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS incentive_slash_events (
  rental_id TEXT PRIMARY KEY,
  node_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  slash_mode TEXT,
  applied_slash_pct INTEGER,
  detected_at_ms INTEGER NOT NULL,
  processed_at_ms INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_incentive_slash_events_unprocessed
  ON incentive_slash_events (processed_at_ms, detected_at_ms);

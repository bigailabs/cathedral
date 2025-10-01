-- Initial miner database schema

-- ============================================================================
-- REGISTRATION TABLES
-- ============================================================================

-- Node health tracking
CREATE TABLE IF NOT EXISTS node_health (
    node_id TEXT PRIMARY KEY,
    is_healthy BOOLEAN NOT NULL DEFAULT FALSE,
    last_health_check TIMESTAMP,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Validator interaction audit trail
CREATE TABLE IF NOT EXISTS validator_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    validator_hotkey TEXT NOT NULL,
    interaction_type TEXT NOT NULL,
    success BOOLEAN NOT NULL,
    details TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- SSH access grants
CREATE TABLE IF NOT EXISTS ssh_access_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    validator_hotkey TEXT NOT NULL,
    node_ids TEXT NOT NULL,
    granted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

-- Node identity (UUID/HUID) tracking
CREATE TABLE IF NOT EXISTS node_uuids (
    node_address TEXT NOT NULL UNIQUE,
    uuid TEXT NOT NULL UNIQUE,
    huid TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- ASSIGNMENT TABLES
-- ============================================================================

-- Manual node assignments to validators
CREATE TABLE IF NOT EXISTS node_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL UNIQUE,
    validator_hotkey TEXT NOT NULL,
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    assigned_by TEXT NOT NULL,
    notes TEXT
);

-- Validator stake tracking
CREATE TABLE IF NOT EXISTS validator_stakes (
    validator_hotkey TEXT PRIMARY KEY,
    stake_amount REAL NOT NULL,
    percentage_of_total REAL NOT NULL,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Assignment audit history
CREATE TABLE IF NOT EXISTS assignment_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    validator_hotkey TEXT,
    action TEXT NOT NULL,
    performed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    performed_by TEXT NOT NULL
);

-- ============================================================================
-- INDEXES
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_validator_interactions_hotkey ON validator_interactions(validator_hotkey);
CREATE INDEX IF NOT EXISTS idx_ssh_grants_validator ON ssh_access_grants(validator_hotkey);

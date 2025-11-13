-- API-level rental tracking (lightweight, just for user queries)

CREATE TABLE community_rentals (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    validator_rental_id TEXT NOT NULL,  -- ID from validator response
    node_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stopped_at TIMESTAMPTZ
);

CREATE TABLE secure_cloud_rentals (
    id TEXT PRIMARY KEY,                -- Our rental ID
    user_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,        -- Aggregator deployment ID
    offering_id TEXT NOT NULL,          -- Original offering from aggregator
    provider TEXT NOT NULL,             -- "datacrunch", "hyperstack", etc.
    ssh_key_id TEXT NOT NULL,           -- Reference to ssh_keys table
    status TEXT NOT NULL DEFAULT 'running',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stopped_at TIMESTAMPTZ,
    FOREIGN KEY (ssh_key_id) REFERENCES ssh_keys(id)
);

-- Indexes
CREATE INDEX idx_community_rentals_user ON community_rentals(user_id);
CREATE INDEX idx_secure_cloud_rentals_user ON secure_cloud_rentals(user_id);
CREATE INDEX idx_secure_cloud_rentals_deployment ON secure_cloud_rentals(deployment_id);

-- Comments
COMMENT ON TABLE community_rentals IS 'API-level tracking for community cloud rentals';
COMMENT ON TABLE secure_cloud_rentals IS 'API-level tracking for secure cloud rentals';

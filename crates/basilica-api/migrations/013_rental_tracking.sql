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
    id TEXT PRIMARY KEY,                    -- Our rental/deployment ID (UUID)
    user_id TEXT NOT NULL,                  -- User ID from Auth0 (matches API auth)
    provider TEXT NOT NULL,                 -- Provider name (datacrunch, hyperstack, etc.)
    provider_instance_id TEXT,              -- Provider's instance ID (set after deployment)
    offering_id TEXT NOT NULL,              -- Reference to gpu_offerings.id
    instance_type TEXT NOT NULL,            -- Provider's instance type identifier
    location_code TEXT,                     -- Deployment location/region
    status TEXT NOT NULL DEFAULT 'running', -- pending, provisioning, running, error, deleted, stopped
    hostname TEXT NOT NULL,                 -- Generated hostname (basilica-{id})
    ssh_key_id TEXT NOT NULL,               -- Reference to ssh_keys table
    ip_address TEXT,                        -- Instance IP address when ready
    connection_info JSONB,                  -- Connection details (SSH, Jupyter, etc.) as JSONB
    raw_response JSONB,                     -- Full provider response as JSONB
    error_message TEXT,                     -- Error message if deployment failed
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL,        -- Last update timestamp
    stopped_at TIMESTAMPTZ,                 -- When rental was stopped
    FOREIGN KEY (ssh_key_id) REFERENCES ssh_keys(id)
);

-- Indexes
CREATE INDEX idx_community_rentals_user ON community_rentals(user_id);
CREATE INDEX idx_secure_cloud_rentals_user ON secure_cloud_rentals(user_id);
CREATE INDEX idx_secure_cloud_rentals_provider ON secure_cloud_rentals(provider);
CREATE INDEX idx_secure_cloud_rentals_status ON secure_cloud_rentals(status);
CREATE INDEX idx_secure_cloud_rentals_provider_instance_id ON secure_cloud_rentals(provider_instance_id);
CREATE INDEX idx_secure_cloud_rentals_created_at ON secure_cloud_rentals(created_at);

-- Comments
COMMENT ON TABLE community_rentals IS 'API-level tracking for community cloud rentals';
COMMENT ON TABLE secure_cloud_rentals IS 'API-level tracking for secure cloud rentals';

-- Deployments table for tracking GPU instance deployments
CREATE TABLE deployments (
    id TEXT PRIMARY KEY,                    -- Our internal deployment ID (UUID)
    user_id TEXT,                           -- User ID from Auth0 via basilica-api
    provider TEXT NOT NULL,                 -- Provider name (datacrunch, hyperstack, etc.)
    provider_instance_id TEXT,              -- Provider's instance ID (set after deployment)
    offering_id TEXT NOT NULL,              -- Reference to gpu_offerings.id
    instance_type TEXT NOT NULL,            -- Provider's instance type identifier
    location_code TEXT,                     -- Deployment location/region
    status TEXT NOT NULL,                   -- pending, provisioning, running, error, deleted
    hostname TEXT NOT NULL,                 -- Generated hostname (basilica-{id})
    ssh_key_id TEXT,                        -- Provider's SSH key ID used
    ip_address TEXT,                        -- Instance IP address when ready
    connection_info TEXT,                   -- JSON with connection details (SSH, Jupyter, etc.)
    raw_response TEXT,                      -- Full provider response (JSON)
    error_message TEXT,                     -- Error message if deployment failed
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_deployments_provider ON deployments(provider);
CREATE INDEX idx_deployments_status ON deployments(status);
CREATE INDEX idx_deployments_provider_instance_id ON deployments(provider_instance_id);
CREATE INDEX idx_deployments_created_at ON deployments(created_at);
CREATE INDEX idx_deployments_user_id ON deployments(user_id);

-- User SSH keys (one per user)
CREATE TABLE ssh_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    public_key TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ssh_keys_user_id ON ssh_keys(user_id);

-- Provider-specific SSH key registrations (lazy)
CREATE TABLE provider_ssh_keys (
    id TEXT PRIMARY KEY,
    ssh_key_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_key_id TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ssh_key_id, provider),
    FOREIGN KEY (ssh_key_id) REFERENCES ssh_keys(id) ON DELETE CASCADE
);

CREATE INDEX idx_provider_ssh_keys_lookup ON provider_ssh_keys(ssh_key_id, provider);

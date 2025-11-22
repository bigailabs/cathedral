-- Create terminated_secure_cloud_rentals table for tracking stopped/terminated secure cloud rental history
CREATE TABLE IF NOT EXISTS terminated_secure_cloud_rentals (
    id TEXT PRIMARY KEY,                    -- Our rental/deployment ID (UUID)
    user_id TEXT NOT NULL,                  -- User ID from Auth0 (matches API auth)
    provider TEXT NOT NULL,                 -- Provider name (datacrunch, hyperstack, etc.)
    provider_instance_id TEXT,              -- Provider's instance ID
    offering_id TEXT NOT NULL,              -- Reference to gpu_offerings.id
    instance_type TEXT NOT NULL,            -- Provider's instance type identifier
    location_code TEXT,                     -- Deployment location/region
    status TEXT NOT NULL,                   -- Final status: deleted, error, stopped
    hostname TEXT NOT NULL,                 -- Generated hostname (basilica-{id})
    ssh_key_id TEXT NOT NULL,               -- Reference to ssh_keys table
    ip_address TEXT,                        -- Instance IP address
    connection_info JSONB,                  -- Connection details (SSH, Jupyter, etc.) as JSONB
    raw_response JSONB,                     -- Full provider response as JSONB
    error_message TEXT,                     -- Error message if deployment failed
    created_at TIMESTAMPTZ NOT NULL,        -- When rental was created
    updated_at TIMESTAMPTZ NOT NULL,        -- Last update before termination
    stopped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- When rental was stopped/terminated
    stop_reason TEXT                        -- Reason for termination (e.g., "User requested stop", "Health check: deployment no longer accessible")
);

-- Index for efficient user-based queries on historical rentals
CREATE INDEX IF NOT EXISTS idx_terminated_secure_cloud_rentals_user_id ON terminated_secure_cloud_rentals(user_id);

-- Index for time-based queries (for analytics, auditing, etc.)
CREATE INDEX IF NOT EXISTS idx_terminated_secure_cloud_rentals_stopped_at ON terminated_secure_cloud_rentals(stopped_at);

-- Composite index for user + time queries (e.g., user's rental history in date range)
CREATE INDEX IF NOT EXISTS idx_terminated_secure_cloud_rentals_user_stopped ON terminated_secure_cloud_rentals(user_id, stopped_at DESC);

-- Index for provider-based queries
CREATE INDEX IF NOT EXISTS idx_terminated_secure_cloud_rentals_provider ON terminated_secure_cloud_rentals(provider);

-- Comments
COMMENT ON TABLE terminated_secure_cloud_rentals IS 'Historical record of terminated/stopped secure cloud rentals';
COMMENT ON COLUMN terminated_secure_cloud_rentals.stop_reason IS 'Reason for termination: user action or health check detection';

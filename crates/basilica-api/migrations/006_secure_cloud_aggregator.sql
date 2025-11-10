-- Secure Cloud GPU Aggregator Tables
-- Migrated from basilica-aggregator SQLite to PostgreSQL

-- GPU offerings table - cached provider pricing data
CREATE TABLE IF NOT EXISTS gpu_offerings (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    gpu_type TEXT NOT NULL, -- GPU category (A100, H100, B200, OTHER)
    gpu_memory_gb_per_gpu INTEGER, -- GPU memory per single GPU card in GB (NULL if provider doesn't specify)
    gpu_count INTEGER NOT NULL,
    interconnect TEXT, -- GPU interconnect type (SXM4, SXM5, SXM6, PCIe, PCIe-NVLink, etc.)
    storage TEXT, -- Storage capacity (raw provider data, no unit conversion)
    deployment_type TEXT, -- Deployment type (vm, bare-metal, container, etc.)
    system_memory_gb INTEGER NOT NULL, -- System RAM
    vcpu_count INTEGER NOT NULL,
    region TEXT NOT NULL,
    hourly_rate DECIMAL(10, 4) NOT NULL,
    availability BOOLEAN NOT NULL,
    raw_metadata JSONB NOT NULL, -- Full provider response as JSONB
    fetched_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gpu_offerings_provider_gpu ON gpu_offerings(provider, gpu_type);
CREATE INDEX IF NOT EXISTS idx_gpu_offerings_fetched_at ON gpu_offerings(fetched_at);
CREATE INDEX IF NOT EXISTS idx_gpu_offerings_region ON gpu_offerings(region);
CREATE INDEX IF NOT EXISTS idx_gpu_offerings_availability ON gpu_offerings(availability);

-- Deployments table for tracking secure cloud GPU instance deployments
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY,                    -- Our internal deployment ID (UUID)
    user_id TEXT NOT NULL,                  -- User ID from Auth0 (matches API auth)
    provider TEXT NOT NULL,                 -- Provider name (datacrunch, hyperstack, etc.)
    provider_instance_id TEXT,              -- Provider's instance ID (set after deployment)
    offering_id TEXT NOT NULL,              -- Reference to gpu_offerings.id
    instance_type TEXT NOT NULL,            -- Provider's instance type identifier
    location_code TEXT,                     -- Deployment location/region
    status TEXT NOT NULL,                   -- pending, provisioning, running, error, deleted
    hostname TEXT NOT NULL,                 -- Generated hostname (basilica-{id})
    ssh_key_id TEXT,                        -- Reference to ssh_keys.id
    ip_address TEXT,                        -- Instance IP address when ready
    connection_info JSONB,                  -- Connection details (SSH, Jupyter, etc.) as JSONB
    raw_response JSONB,                     -- Full provider response as JSONB
    error_message TEXT,                     -- Error message if deployment failed
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deployments_provider ON deployments(provider);
CREATE INDEX IF NOT EXISTS idx_deployments_status ON deployments(status);
CREATE INDEX IF NOT EXISTS idx_deployments_provider_instance_id ON deployments(provider_instance_id);
CREATE INDEX IF NOT EXISTS idx_deployments_created_at ON deployments(created_at);
CREATE INDEX IF NOT EXISTS idx_deployments_user_id ON deployments(user_id);

-- User SSH keys (one per user for secure cloud deployments)
CREATE TABLE IF NOT EXISTS ssh_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,          -- Matches Auth0 user ID from API
    name TEXT NOT NULL,
    public_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ssh_keys_user_id ON ssh_keys(user_id);

-- Provider-specific SSH key registrations (lazy registration on first use)
CREATE TABLE IF NOT EXISTS provider_ssh_keys (
    id TEXT PRIMARY KEY,
    ssh_key_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_key_id TEXT NOT NULL,         -- Provider's SSH key ID
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(ssh_key_id, provider),
    FOREIGN KEY (ssh_key_id) REFERENCES ssh_keys(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_provider_ssh_keys_lookup ON provider_ssh_keys(ssh_key_id, provider);

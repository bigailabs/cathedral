-- Secure Cloud GPU Aggregator Tables
-- Tables for basilica-aggregator service (uses shared API PostgreSQL database)

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

-- Note: Deployments are tracked in the secure_cloud_rentals table (see migration 013)
-- This consolidates rental tracking and deployment tracking into a single table

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

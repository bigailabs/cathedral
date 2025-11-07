-- GPU offerings table
CREATE TABLE gpu_offerings (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    gpu_type TEXT NOT NULL, -- GPU category (A100, H100, B200, OTHER)
    gpu_memory_gb INTEGER NOT NULL, -- GPU memory per card
    gpu_count INTEGER NOT NULL,
    interconnect TEXT, -- GPU interconnect type (SXM4, SXM5, SXM6, PCIe, PCIe-NVLink, etc.)
    storage TEXT, -- Storage capacity (raw provider data, no unit conversion)
    deployment_type TEXT, -- Deployment type (vm, bare-metal, container, etc.)
    system_memory_gb INTEGER NOT NULL, -- System RAM
    vcpu_count INTEGER NOT NULL,
    region TEXT NOT NULL,
    hourly_rate TEXT NOT NULL,
    spot_rate TEXT,
    availability BOOLEAN NOT NULL,
    raw_metadata TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_provider_gpu ON gpu_offerings(provider, gpu_type);
CREATE INDEX idx_fetched_at ON gpu_offerings(fetched_at);
CREATE INDEX idx_region ON gpu_offerings(region);
CREATE INDEX idx_availability ON gpu_offerings(availability);

-- Provider status tracking
CREATE TABLE provider_status (
    provider TEXT PRIMARY KEY,
    last_fetch_at TIMESTAMP,
    last_success_at TIMESTAMP,
    last_error TEXT,
    is_healthy BOOLEAN NOT NULL DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

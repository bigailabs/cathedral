-- Migration: Create deployment instance mappings table
-- Purpose: Map (user_id, instance_name) to a stable instance_id (UUID)
-- This enables idempotent deployments where re-deploying with the same
-- instance_name reuses the same storage prefix.

CREATE TABLE IF NOT EXISTS deployment_instance_mappings (
    user_id VARCHAR(255) NOT NULL,
    instance_name VARCHAR(63) NOT NULL,
    instance_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    PRIMARY KEY (user_id, instance_name),
    CONSTRAINT uq_instance_id UNIQUE(instance_id)
);

CREATE INDEX IF NOT EXISTS idx_dim_user_id ON deployment_instance_mappings(user_id);
CREATE INDEX IF NOT EXISTS idx_dim_instance_id ON deployment_instance_mappings(instance_id);

COMMENT ON TABLE deployment_instance_mappings IS 'Maps user instance names to stable UUIDs for storage persistence';
COMMENT ON COLUMN deployment_instance_mappings.instance_name IS 'User-provided friendly name for the deployment';
COMMENT ON COLUMN deployment_instance_mappings.instance_id IS 'Stable UUID used as storage prefix and K8s resource identifier';

-- Create user_deployments table for tracking on-demand K8s deployments
CREATE TABLE IF NOT EXISTS user_deployments (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    instance_name VARCHAR(63) NOT NULL,
    namespace VARCHAR(63) NOT NULL,
    cr_name VARCHAR(63) NOT NULL,

    image TEXT NOT NULL,
    replicas INTEGER NOT NULL,
    port INTEGER NOT NULL,

    path_prefix TEXT NOT NULL,
    public_url TEXT NOT NULL,

    state VARCHAR(50) NOT NULL DEFAULT 'Pending',
    message TEXT,

    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMP WITH TIME ZONE,

    CONSTRAINT uq_user_deployment_instance UNIQUE(user_id, instance_name),
    CONSTRAINT chk_replicas_positive CHECK (replicas > 0),
    CONSTRAINT chk_port_range CHECK (port > 0 AND port <= 65535),
    CONSTRAINT chk_state_valid CHECK (state IN ('Pending', 'Active', 'Failed', 'Terminating', 'Deleted'))
);

-- Index for efficient user-based queries
CREATE INDEX IF NOT EXISTS idx_user_deployments_user_id ON user_deployments(user_id);

-- Index for state-based queries (listing active deployments)
CREATE INDEX IF NOT EXISTS idx_user_deployments_state ON user_deployments(state);

-- Index for time-based queries (cleanup, analytics)
CREATE INDEX IF NOT EXISTS idx_user_deployments_created_at ON user_deployments(created_at);

-- Composite index for user + state queries (most common query pattern)
CREATE INDEX IF NOT EXISTS idx_user_deployments_user_state ON user_deployments(user_id, state)
WHERE state IN ('Pending', 'Active');

-- Index for soft-deleted deployments
CREATE INDEX IF NOT EXISTS idx_user_deployments_deleted_at ON user_deployments(deleted_at)
WHERE deleted_at IS NOT NULL;

-- Trigger function to update updated_at column
CREATE OR REPLACE FUNCTION update_user_deployments_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger to automatically update updated_at on row updates
DROP TRIGGER IF EXISTS user_deployments_updated_at_trigger ON user_deployments;
CREATE TRIGGER user_deployments_updated_at_trigger
    BEFORE UPDATE ON user_deployments
    FOR EACH ROW
    EXECUTE FUNCTION update_user_deployments_updated_at();

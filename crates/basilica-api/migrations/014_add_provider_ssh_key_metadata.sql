-- Add metadata column to provider_ssh_keys table
-- This stores provider-specific data like key names, fingerprints, etc.

ALTER TABLE provider_ssh_keys
ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT NULL;

COMMENT ON COLUMN provider_ssh_keys.metadata IS 'Provider-specific metadata (e.g., Hyperstack key name, fingerprints, etc.)';

-- Add ephemeral mount path to miner_nodes
-- Stores the host path where ephemeral storage is mounted (e.g., "/ephemeral")
-- NULL means no ephemeral storage declared by the miner
ALTER TABLE miner_nodes ADD COLUMN ephemeral_mount_path TEXT;

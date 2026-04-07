-- Add extra mount path to miner_nodes
-- Stores the host path where extra storage is mounted (e.g., "/ephemeral")
-- NULL means no extra storage declared by the miner
ALTER TABLE miner_nodes ADD COLUMN extra_mount_path TEXT;

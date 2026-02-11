-- Add node_ip column for efficient uniqueness checks (avoids parsing ssh_endpoint at runtime)
ALTER TABLE miner_nodes ADD COLUMN node_ip TEXT NOT NULL DEFAULT '';

-- Backfill: extract host from ssh_endpoint format "user@host:port"
UPDATE miner_nodes SET node_ip = SUBSTR(
    SUBSTR(ssh_endpoint, INSTR(ssh_endpoint, '@') + 1),
    1,
    INSTR(SUBSTR(ssh_endpoint, INSTR(ssh_endpoint, '@') + 1), ':') - 1
)
WHERE ssh_endpoint LIKE '%@%:%';

-- Index for uniqueness lookups
CREATE INDEX idx_miner_nodes_node_ip ON miner_nodes(node_ip);

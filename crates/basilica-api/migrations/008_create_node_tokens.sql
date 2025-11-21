CREATE TABLE IF NOT EXISTS node_cluster_tokens (
  user_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  token_secret TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (user_id, node_id)
);

CREATE INDEX idx_node_cluster_tokens_expires_at ON node_cluster_tokens(expires_at);

CREATE INDEX idx_node_cluster_tokens_token_id ON node_cluster_tokens(token_id);

COMMENT ON TABLE node_cluster_tokens IS 'K3s cluster join tokens for GPU nodes with 1-hour TTL';

//! Node management module for direct node access
//!
//! This module manages the nodes that the miner offers to validators.
//! Nodes are compute resources with SSH access that validators can use directly.

use anyhow::{Context, Result};
use basilica_common::ssh::{
    SshConnectionConfig, SshConnectionDetails, SshConnectionManager, StandardSshClient,
};
use basilica_protocol::miner_discovery::{
    DiscoverNodesRequest, ListNodeConnectionDetailsResponse, NodeConnectionDetails,
};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use tracing::{debug, info, warn};

use crate::config::NodeSshConfig;

/// Configuration for a single node
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct NodeConfig {
    /// SSH hostname or IP address
    pub host: String,
    /// SSH port (typically 22)
    pub port: u16,
    /// SSH username for validator access
    pub username: String,
    /// Additional SSH options
    pub additional_opts: Option<String>,
}

impl NodeConfig {
    /// Convert to SSH connection details for remote command execution
    fn to_ssh_connection_details(&self, private_key_path: PathBuf) -> SshConnectionDetails {
        SshConnectionDetails {
            host: self.host.clone(),
            username: self.username.clone(),
            port: self.port,
            private_key_path,
            timeout: Duration::from_secs(30),
        }
    }
}

/// Manages nodes available for rental
pub struct NodeManager {
    /// Map of node_id to node configuration
    nodes: Arc<RwLock<HashMap<String, NodeConfig>>>,
    /// SSH public keys of authorized validators
    authorized_validators: Arc<RwLock<HashMap<String, String>>>,
    /// SSH client for executing remote commands
    ssh_client: Arc<StandardSshClient>,
    /// SSH configuration
    ssh_config: NodeSshConfig,
}

/// Node configuration with generated ID
#[derive(Clone, Debug)]
pub struct RegisteredNode {
    pub node_id: String,
    pub config: NodeConfig,
}

impl std::fmt::Debug for NodeManager {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NodeManager")
            .field("nodes", &"<Arc<RwLock<HashMap>>>")
            .field("authorized_validators", &"<Arc<RwLock<HashMap>>>")
            .field("ssh_client", &"<StandardSshClient>")
            .finish()
    }
}

impl Default for NodeManager {
    fn default() -> Self {
        Self::new(NodeSshConfig::default())
    }
}

impl NodeManager {
    /// Create a new node manager
    pub fn new(ssh_config: NodeSshConfig) -> Self {
        // Use permissive SSH config to avoid host key verification issues
        let config = SshConnectionConfig {
            strict_host_key_checking: false,
            known_hosts_file: None,
            ..Default::default()
        };
        Self {
            nodes: Arc::new(RwLock::new(HashMap::new())),
            authorized_validators: Arc::new(RwLock::new(HashMap::new())),
            ssh_client: Arc::new(StandardSshClient::with_config(config)),
            ssh_config,
        }
    }

    /// Register a node for availability with an auto-generated node_id
    pub async fn register_node(&self, node_id: String, config: NodeConfig) -> Result<()> {
        let mut nodes = self.nodes.write().await;
        info!(
            "Registering node {} at {}:{}",
            node_id, config.host, config.port
        );
        nodes.insert(node_id, config);
        Ok(())
    }

    /// Remove a node from availability
    pub async fn unregister_node(&self, node_id: &str) -> Result<()> {
        let mut nodes = self.nodes.write().await;
        if nodes.remove(node_id).is_some() {
            info!("Unregistered node {}", node_id);
        } else {
            warn!("Attempted to unregister unknown node {}", node_id);
        }
        Ok(())
    }

    /// Get all available nodes with their IDs
    pub async fn list_nodes(&self) -> Result<Vec<RegisteredNode>> {
        let nodes = self.nodes.read().await;
        Ok(nodes
            .iter()
            .map(|(node_id, config)| RegisteredNode {
                node_id: node_id.clone(),
                config: config.clone(),
            })
            .collect())
    }

    /// Get a specific node by ID
    pub async fn get_node(&self, node_id: &str) -> Result<Option<NodeConfig>> {
        let nodes = self.nodes.read().await;
        Ok(nodes.get(node_id).cloned())
    }

    /// Normalize SSH public key by extracting algorithm + key and adding our identifier
    pub fn normalize_ssh_key(ssh_public_key: &str, validator_hotkey: &str) -> String {
        let parts: Vec<&str> = ssh_public_key.split_whitespace().collect();

        if parts.len() >= 2 {
            // Keep only algorithm and base64 key, add our identifier as comment
            format!("{} {} validator-{}", parts[0], parts[1], validator_hotkey)
        } else {
            // Fallback if format is unexpected
            format!("{} validator-{}", ssh_public_key.trim(), validator_hotkey)
        }
    }

    /// Extract core key (algorithm + base64) for comparison
    pub fn extract_key_core(ssh_public_key: &str) -> String {
        let parts: Vec<&str> = ssh_public_key.split_whitespace().collect();

        if parts.len() >= 2 {
            format!("{} {}", parts[0], parts[1])
        } else {
            ssh_public_key.trim().to_string()
        }
    }

    /// Authorize a validator's SSH public key and deploy it to all nodes
    pub async fn authorize_validator(
        &self,
        validator_hotkey: &str,
        ssh_public_key: &str,
    ) -> Result<()> {
        // Validate SSH public key format
        if !self.is_valid_ssh_public_key(ssh_public_key) {
            return Err(anyhow::anyhow!("Invalid SSH public key format"));
        }

        // Normalize the key with our identifier
        let normalized_key = Self::normalize_ssh_key(ssh_public_key, validator_hotkey);

        // Get all enabled nodes
        let nodes = self.list_nodes().await?;

        // Get the miner's SSH private key path from config
        let private_key_path = self.get_ssh_key_path();

        // Deploy the SSH key to each node, ensuring exclusive access
        for registered_node in &nodes {
            info!(
                "Setting exclusive SSH access for validator {} on node {}",
                validator_hotkey, registered_node.node_id
            );

            // Build SSH connection details
            let connection_details = registered_node
                .config
                .to_ssh_connection_details(private_key_path.clone());

            // Set exclusive validator key (removes all other validators, adds current one)
            self.set_exclusive_validator_key_on_node(
                &connection_details,
                &registered_node.node_id,
                validator_hotkey,
                &normalized_key,
            )
            .await?;
        }

        // Store in memory for tracking
        let mut validators = self.authorized_validators.write().await;
        validators.insert(validator_hotkey.to_string(), ssh_public_key.to_string());

        info!(
            "Authorized validator {} with SSH key on {} nodes",
            validator_hotkey,
            nodes.len()
        );

        Ok(())
    }

    /// Revoke a validator's authorization and remove their SSH key from all nodes
    pub async fn revoke_validator(&self, validator_hotkey: &str) -> Result<()> {
        info!("Revoking validator {} authorization", validator_hotkey);

        // Get all nodes
        let nodes = self.list_nodes().await?;

        // Get the miner's SSH private key path from config
        let private_key_path = self.get_ssh_key_path();

        // Remove all validator keys from each node
        for registered_node in &nodes {
            info!(
                "Removing all validator keys from node {} (revoking validator {})",
                registered_node.node_id, validator_hotkey
            );

            // Build SSH connection details
            let connection_details = registered_node
                .config
                .to_ssh_connection_details(private_key_path.clone());

            // Remove all validator keys from the node
            if let Err(e) = self
                .remove_all_validator_keys_on_node(&connection_details, &registered_node.node_id)
                .await
            {
                warn!(
                    "Failed to remove validator keys from node {}: {}",
                    registered_node.node_id, e
                );
            }
        }

        // Remove from memory
        let mut validators = self.authorized_validators.write().await;
        validators.remove(validator_hotkey);

        info!(
            "Revoked validator {} authorization from {} nodes",
            validator_hotkey,
            nodes.len()
        );

        Ok(())
    }

    /// Check if a validator is authorized
    pub async fn is_validator_authorized(&self, validator_hotkey: &str) -> bool {
        let validators = self.authorized_validators.read().await;
        validators.contains_key(validator_hotkey)
    }

    /// Handle DiscoverNodes request from validator
    pub async fn handle_discover_nodes(
        &self,
        request: DiscoverNodesRequest,
    ) -> Result<ListNodeConnectionDetailsResponse> {
        // Verify the validator is providing an SSH public key
        if request.validator_public_key.is_empty() {
            return Err(anyhow::anyhow!("Validator must provide SSH public key"));
        }

        // Authorize the validator's SSH key on all nodes
        self.authorize_validator(&request.validator_hotkey, &request.validator_public_key)
            .await
            .context("Failed to authorize validator")?;

        // Get all available nodes
        let nodes = self.list_nodes().await?;

        // Convert to protocol format
        let node_details: Vec<NodeConnectionDetails> = nodes
            .into_iter()
            .map(|registered_node| NodeConnectionDetails {
                node_id: registered_node.node_id,
                host: registered_node.config.host,
                port: registered_node.config.port.to_string(),
                username: registered_node.config.username,
                additional_opts: registered_node.config.additional_opts.unwrap_or_default(),
                gpu_spec: None, // Validators discover GPU specs via SSH
                status: "available".to_string(),
            })
            .collect();

        Ok(ListNodeConnectionDetailsResponse {
            nodes: node_details,
        })
    }

    /// Validate SSH public key format
    fn is_valid_ssh_public_key(&self, public_key: &str) -> bool {
        public_key.starts_with("ssh-rsa ")
            || public_key.starts_with("ssh-ed25519 ")
            || public_key.starts_with("ecdsa-sha2-")
            || public_key.starts_with("ssh-dss ")
    }

    /// Get the SSH private key path from config or environment
    fn get_ssh_key_path(&self) -> PathBuf {
        // First check if the configured path exists
        let configured_path = &self.ssh_config.miner_node_key_path;

        // Expand tilde if present
        let expanded_path = if configured_path.starts_with("~") {
            if let Ok(home) = std::env::var("HOME") {
                PathBuf::from(configured_path.to_string_lossy().replacen('~', &home, 1))
            } else {
                configured_path.clone()
            }
        } else {
            configured_path.clone()
        };

        // If the configured path exists, use it
        if expanded_path.exists() {
            return expanded_path;
        }

        // Otherwise, try environment variable
        if let Ok(env_path) = std::env::var("MINER_SSH_KEY_PATH") {
            let env_path = PathBuf::from(env_path);
            if env_path.exists() {
                return env_path;
            }
        }

        // Finally, fall back to default ~/.ssh/id_rsa
        if let Ok(home) = std::env::var("HOME") {
            PathBuf::from(format!("{}/.ssh/id_rsa", home))
        } else {
            PathBuf::from("/root/.ssh/id_rsa")
        }
    }

    /// Remove all validator keys from a node's authorized_keys file
    async fn remove_all_validator_keys_on_node(
        &self,
        connection_details: &SshConnectionDetails,
        node_id: &str,
    ) -> Result<()> {
        debug!("Removing all validator keys from node {}", node_id);

        let ssh_command = "mkdir -p ~/.ssh && (grep -v 'validator-' ~/.ssh/authorized_keys 2>/dev/null || echo -n '') > ~/.ssh/authorized_keys.tmp && mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys";

        self.ssh_client
            .execute_command(connection_details, ssh_command, false)
            .await
            .map_err(|e| {
                anyhow::anyhow!(
                    "Failed to remove validator keys from node {}: {}",
                    node_id,
                    e
                )
            })?;

        debug!(
            "Successfully removed all validator keys from node {}",
            node_id
        );
        Ok(())
    }

    /// Set exclusive validator key on a node (removes all other validators, adds current one)
    async fn set_exclusive_validator_key_on_node(
        &self,
        connection_details: &SshConnectionDetails,
        node_id: &str,
        validator_hotkey: &str,
        normalized_key: &str,
    ) -> Result<()> {
        // Atomic operation: remove all validator keys and add the new one
        let ssh_command = format!(
            "mkdir -p ~/.ssh && (grep -v 'validator-' ~/.ssh/authorized_keys 2>/dev/null || echo -n '') > ~/.ssh/authorized_keys.tmp && printf '%s\\n' '{}' >> ~/.ssh/authorized_keys.tmp && mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys",
            normalized_key
        );

        self.ssh_client
            .execute_command(connection_details, &ssh_command, false)
            .await
            .map_err(|e| {
                anyhow::anyhow!(
                    "Failed to set exclusive validator key on node {}: {}",
                    node_id,
                    e
                )
            })?;

        info!(
            "Successfully set exclusive access for validator {} on node {}",
            validator_hotkey, node_id
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_node_registration() {
        let manager = NodeManager::new(NodeSshConfig::default());

        let config = NodeConfig {
            host: "192.168.1.100".to_string(),
            port: 22,
            username: "basilica".to_string(),
            additional_opts: None,
        };

        let node_id = "test-node-1".to_string();
        manager
            .register_node(node_id.clone(), config.clone())
            .await
            .unwrap();

        let nodes = manager.list_nodes().await.unwrap();
        assert_eq!(nodes.len(), 1);
        assert_eq!(nodes[0].node_id, "test-node-1");

        let node = manager.get_node("test-node-1").await.unwrap();
        assert!(node.is_some());
        assert_eq!(node.unwrap().host, "192.168.1.100");
    }

    #[tokio::test]
    async fn test_validator_authorization() {
        let manager = NodeManager::new(NodeSshConfig::default());

        let validator_key = "validator-hotkey-123";
        let ssh_key = "ssh-rsa AAAAB3NzaC1yc2E...";

        // Without any nodes, this should succeed but not deploy anywhere
        manager
            .authorize_validator(validator_key, ssh_key)
            .await
            .unwrap();

        assert!(manager.is_validator_authorized(validator_key).await);
        assert!(!manager.is_validator_authorized("unknown-validator").await);
    }

    #[tokio::test]
    async fn test_discover_nodes_request() {
        let manager = NodeManager::new(NodeSshConfig::default());

        // Register a node
        let config = NodeConfig {
            host: "10.0.0.50".to_string(),
            port: 22,
            username: "gpu_user".to_string(),
            additional_opts: Some("-o StrictHostKeyChecking=no".to_string()),
        };

        let node_id = "gpu-node-1".to_string();
        manager.register_node(node_id, config).await.unwrap();

        // Create a discovery request
        let request = DiscoverNodesRequest {
            validator_hotkey: "validator-123".to_string(),
            signature: "signature".to_string(),
            nonce: "nonce".to_string(),
            validator_public_key: "ssh-rsa AAAAB3NzaC1yc2E...".to_string(),
            timestamp: None,
            target_miner_hotkey: "miner-456".to_string(),
        };

        // Note: This will try to SSH to 10.0.0.50, which won't work in tests
        // In a real system, you'd mock the SSH client or use a test double
        let result = manager.handle_discover_nodes(request).await;

        // The test will fail when trying to SSH, which is expected
        assert!(result.is_err());
    }
}

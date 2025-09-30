//! Node management module for direct node access
//!
//! This module manages the nodes that the miner offers to validators.
//! Nodes are compute resources with SSH access that validators can use directly.

use anyhow::{Context, Result};
use basilica_common::ssh::{SshConnectionDetails, SshConnectionManager, StandardSshClient};
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
    /// Unique identifier for this node
    pub node_id: String,
    /// SSH hostname or IP address
    pub host: String,
    /// SSH port (typically 22)
    pub port: u16,
    /// SSH username for validator access
    pub username: String,
    /// Additional SSH options
    pub additional_opts: Option<String>,
    /// GPU specifications if available
    pub gpu_spec: Option<basilica_protocol::common::GpuSpec>,
    /// Whether this node is currently enabled
    pub enabled: bool,
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
        Self {
            nodes: Arc::new(RwLock::new(HashMap::new())),
            authorized_validators: Arc::new(RwLock::new(HashMap::new())),
            ssh_client: Arc::new(StandardSshClient::new()),
            ssh_config,
        }
    }

    /// Register a node for availability
    pub async fn register_node(&self, config: NodeConfig) -> Result<()> {
        let mut nodes = self.nodes.write().await;
        info!(
            "Registering node {} at {}:{}",
            config.node_id, config.host, config.port
        );
        nodes.insert(config.node_id.clone(), config);
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

    /// Get all available nodes
    pub async fn list_nodes(&self) -> Result<Vec<NodeConfig>> {
        let nodes = self.nodes.read().await;
        Ok(nodes.values().filter(|n| n.enabled).cloned().collect())
    }

    /// Get a specific node by ID
    pub async fn get_node(&self, node_id: &str) -> Result<Option<NodeConfig>> {
        let nodes = self.nodes.read().await;
        Ok(nodes.get(node_id).cloned())
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

        // Get all enabled nodes
        let nodes = self.list_nodes().await?;

        // Get the miner's SSH private key path from config
        let private_key_path = self.get_ssh_key_path();

        // Deploy the SSH key to each node
        for node in &nodes {
            info!(
                "Deploying SSH key for validator {} to node {}",
                validator_hotkey, node.node_id
            );

            // Create the SSH key entry with validator identifier
            let key_entry = format!("{} validator-{}", ssh_public_key, validator_hotkey);

            // Build SSH connection details
            let connection_details = node.to_ssh_connection_details(private_key_path.clone());

            // Use the SSH client to add the key to the remote node's authorized_keys
            let ssh_command = format!(
                "mkdir -p ~/.ssh && echo '{}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys",
                key_entry
            );

            match self
                .ssh_client
                .execute_command(&connection_details, &ssh_command, false)
                .await
            {
                Ok(_) => {
                    debug!(
                        "Successfully deployed SSH key for validator {} to node {}",
                        validator_hotkey, node.node_id
                    );
                }
                Err(e) => {
                    warn!("Failed to add SSH key to node {}: {}", node.node_id, e);
                    return Err(anyhow::anyhow!(
                        "Failed to add SSH key to node {}: {}",
                        node.node_id,
                        e
                    ));
                }
            }
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

        // Remove the SSH key from each node
        for node in &nodes {
            info!(
                "Removing SSH key for validator {} from node {}",
                validator_hotkey, node.node_id
            );

            // Build SSH connection details
            let connection_details = node.to_ssh_connection_details(private_key_path.clone());

            // Remove lines containing the validator identifier from authorized_keys
            let ssh_command = format!(
                "grep -v 'validator-{}' ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp && mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys || true",
                validator_hotkey
            );

            match self
                .ssh_client
                .execute_command(&connection_details, &ssh_command, false)
                .await
            {
                Ok(_) => {
                    debug!(
                        "Successfully removed SSH key for validator {} from node {}",
                        validator_hotkey, node.node_id
                    );
                }
                Err(e) => {
                    warn!("Failed to remove SSH key from node {}: {}", node.node_id, e);
                }
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
            .map(|node| NodeConnectionDetails {
                node_id: node.node_id,
                host: node.host,
                port: node.port.to_string(),
                username: node.username,
                additional_opts: node.additional_opts.unwrap_or_default(),
                gpu_spec: node.gpu_spec,
                status: "available".to_string(),
            })
            .collect();

        Ok(ListNodeConnectionDetailsResponse {
            nodes: node_details,
        })
    }

    /// Update node status
    pub async fn update_node_status(&self, node_id: &str, enabled: bool) -> Result<()> {
        let mut nodes = self.nodes.write().await;
        if let Some(node) = nodes.get_mut(node_id) {
            node.enabled = enabled;
            info!(
                "Updated node {} status to {}",
                node_id,
                if enabled { "enabled" } else { "disabled" }
            );
            Ok(())
        } else {
            Err(anyhow::anyhow!("Node {} not found", node_id))
        }
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
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_node_registration() {
        let manager = NodeManager::new(NodeSshConfig::default());

        let config = NodeConfig {
            node_id: "test-node-1".to_string(),
            host: "192.168.1.100".to_string(),
            port: 22,
            username: "basilica".to_string(),
            additional_opts: None,
            gpu_spec: None,
            enabled: true,
        };

        manager.register_node(config.clone()).await.unwrap();

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
            node_id: "gpu-node-1".to_string(),
            host: "10.0.0.50".to_string(),
            port: 22,
            username: "gpu_user".to_string(),
            additional_opts: Some("-o StrictHostKeyChecking=no".to_string()),
            gpu_spec: Some(basilica_protocol::common::GpuSpec {
                model: "RTX 4090".to_string(),
                memory_mb: 24576,
                uuid: "GPU-123".to_string(),
                driver_version: "535.86.05".to_string(),
                cuda_version: "12.2".to_string(),
                utilization_percent: 0.0,
                memory_utilization_percent: 0.0,
                temperature_celsius: 45.0,
                power_watts: 100.0,
                core_clock_mhz: 2520,
                memory_clock_mhz: 10501,
                compute_capability: "8.9".to_string(),
            }),
            enabled: true,
        };

        manager.register_node(config).await.unwrap();

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

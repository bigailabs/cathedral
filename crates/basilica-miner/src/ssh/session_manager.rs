//! SSH key authorization management for validator access to nodes
//!
//! This module handles the direct SSH access model where validators provide their own
//! SSH public keys and get direct access to nodes without intermediary sessions.

use anyhow::{Context, Result};
use basilica_common::ssh::{SshConnectionDetails, SshConnectionManager, StandardSshClient};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use tracing::{debug, info, warn};

use crate::config::NodeSshConfig;
use crate::persistence::RegistrationDb;

/// Information about a validator's SSH key authorization
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ValidatorKeyAuthorization {
    /// Validator hotkey that owns this authorization
    pub validator_hotkey: String,
    /// SSH public key of the validator
    pub validator_public_key: String,
    /// Node IDs this key is authorized for
    pub authorized_nodes: Vec<String>,
    /// Authorization creation time
    pub created_at: DateTime<Utc>,
    /// Authorization expiration time (if any)
    pub expires_at: Option<DateTime<Utc>>,
    /// SSH username for the validator to use
    pub ssh_username: String,
}

impl ValidatorKeyAuthorization {
    /// Check if authorization is expired
    pub fn is_expired(&self) -> bool {
        if let Some(expires_at) = self.expires_at {
            Utc::now() >= expires_at
        } else {
            false
        }
    }

    /// Get connection string for a specific node
    pub fn get_connection_string(&self, node_host: &str, node_port: u16) -> String {
        format!("ssh {}@{} -p {}", self.ssh_username, node_host, node_port)
    }
}

/// Manages SSH key authorization for validator access to nodes
pub struct SshSessionManager {
    config: NodeSshConfig,
    /// Maps validator hotkey to their authorization
    authorizations: Arc<RwLock<HashMap<String, ValidatorKeyAuthorization>>>,
    db: RegistrationDb,
    /// SSH client for executing remote commands
    ssh_client: Arc<StandardSshClient>,
}

impl SshSessionManager {
    /// Create a new SSH session manager
    pub async fn new(config: NodeSshConfig, db: RegistrationDb) -> Result<Self> {
        let manager = Self {
            config,
            authorizations: Arc::new(RwLock::new(HashMap::new())),
            db,
            ssh_client: Arc::new(StandardSshClient::new()),
        };

        info!("SSH key authorization manager initialized");
        Ok(manager)
    }

    /// Get the miner's SSH private key path from config
    fn get_miner_ssh_key_path(&self) -> PathBuf {
        // First check if the configured path exists
        let configured_path = &self.config.miner_node_key_path;

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

    /// Create SSH connection details for a node host
    fn create_connection_details(&self, node_host: &str, username: &str) -> SshConnectionDetails {
        // Parse host and port from node_host string (format: "host:port" or just "host")
        let (host, port) = if let Some(colon_idx) = node_host.rfind(':') {
            let port_str = &node_host[colon_idx + 1..];
            if let Ok(port) = port_str.parse::<u16>() {
                (node_host[..colon_idx].to_string(), port)
            } else {
                (node_host.to_string(), 22)
            }
        } else {
            (node_host.to_string(), 22)
        };

        SshConnectionDetails {
            host,
            username: username.to_string(),
            port,
            private_key_path: self.get_miner_ssh_key_path(),
            timeout: Duration::from_secs(30),
        }
    }

    /// Authorize a validator's SSH public key for access to specific nodes
    pub async fn authorize_validator_key(
        &self,
        validator_hotkey: &str,
        validator_public_key: &str,
        node_hosts: &[String],
        duration: Option<Duration>,
    ) -> Result<ValidatorKeyAuthorization> {
        info!(
            "Authorizing SSH key for validator {} to nodes: {:?}",
            validator_hotkey, node_hosts
        );

        // Validate SSH public key format
        if !self.is_valid_ssh_public_key(validator_public_key) {
            return Err(anyhow::anyhow!("Invalid SSH public key format"));
        }

        let now = Utc::now();
        let expires_at = duration.map(|d| now + chrono::Duration::from_std(d).unwrap());

        // Use a standard username for validators
        let ssh_username = self.config.default_node_username.clone();

        // Add the public key to authorized_keys on each node
        for node_host in node_hosts {
            debug!("Adding SSH key to authorized_keys on node {}", node_host);

            // Add key with expiration comment if applicable
            let key_entry = if let Some(exp) = expires_at {
                format!(
                    "{} validator-{} expires={}",
                    validator_public_key,
                    validator_hotkey,
                    exp.to_rfc3339()
                )
            } else {
                format!("{} validator-{}", validator_public_key, validator_hotkey)
            };

            // Create connection details for this node
            let connection_details = self.create_connection_details(node_host, &ssh_username);

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
                        "Successfully added SSH key for validator {} to node {}",
                        validator_hotkey, node_host
                    );
                }
                Err(e) => {
                    warn!("Failed to add SSH key to node {}: {}", node_host, e);
                    return Err(anyhow::anyhow!(
                        "Failed to add SSH key to node {}: {}",
                        node_host,
                        e
                    ));
                }
            }
        }

        // Create authorization record
        let authorization = ValidatorKeyAuthorization {
            validator_hotkey: validator_hotkey.to_string(),
            validator_public_key: validator_public_key.to_string(),
            authorized_nodes: node_hosts.to_vec(),
            created_at: now,
            expires_at,
            ssh_username,
        };

        // Store authorization
        {
            let mut authorizations = self.authorizations.write().await;
            authorizations.insert(validator_hotkey.to_string(), authorization.clone());
        }

        // Record in database
        self.db
            .record_ssh_key_authorization(
                validator_hotkey,
                validator_public_key,
                &node_hosts.join(","),
                expires_at.as_ref(),
            )
            .await
            .context("Failed to record SSH authorization in database")?;

        info!(
            "SSH key authorized for validator {} -> nodes {:?} (expires: {:?})",
            validator_hotkey, node_hosts, expires_at
        );

        Ok(authorization)
    }

    /// Revoke a validator's SSH key authorization
    pub async fn revoke_validator_key(
        &self,
        validator_hotkey: &str,
        node_hosts: Option<&[String]>,
    ) -> Result<()> {
        info!("Revoking SSH key for validator {}", validator_hotkey);

        let authorization = {
            let mut authorizations = self.authorizations.write().await;
            let auth = authorizations.get_mut(validator_hotkey);

            match (node_hosts, auth) {
                (Some(nodes_to_revoke), Some(auth)) => {
                    let nodes_to_revoke_set: std::collections::HashSet<_> =
                        nodes_to_revoke.iter().cloned().collect();
                    auth.authorized_nodes
                        .retain(|n| !nodes_to_revoke_set.contains(n));
                    if auth.authorized_nodes.is_empty() {
                        // If no nodes left, remove the entire authorization
                        authorizations.remove(validator_hotkey)
                    } else {
                        Some(auth.clone())
                    }
                }
                (None, _) => {
                    // Remove complete authorization
                    authorizations.remove(validator_hotkey)
                }
                _ => None,
            }
        };

        if let Some(auth) = authorization {
            // Remove the key from authorized_keys on each node
            let nodes_to_revoke = node_hosts.unwrap_or(&auth.authorized_nodes);
            let ssh_username = auth.ssh_username;

            for node_host in nodes_to_revoke {
                info!(
                    "Removing SSH key for validator {} from node {}",
                    validator_hotkey, node_host
                );

                // Create connection details for this node
                let connection_details = self.create_connection_details(node_host, &ssh_username);

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
                            validator_hotkey, node_host
                        );
                    }
                    Err(e) => {
                        warn!("Failed to revoke SSH key from node {}: {}", node_host, e);
                    }
                }
            }

            // Record revocation in database
            self.db
                .record_ssh_key_revocation(validator_hotkey, "manual_revocation")
                .await
                .context("Failed to record SSH revocation")?;

            info!("SSH key revoked for validator {}", validator_hotkey);
        } else {
            warn!(
                "Attempted to revoke non-existent authorization for {}",
                validator_hotkey
            );
        }

        Ok(())
    }

    /// Get authorization for a validator
    pub async fn get_validator_authorization(
        &self,
        validator_hotkey: &str,
    ) -> Result<Option<ValidatorKeyAuthorization>> {
        let authorizations = self.authorizations.read().await;
        Ok(authorizations.get(validator_hotkey).cloned())
    }

    /// List all active authorizations
    pub async fn list_authorizations(&self) -> Result<Vec<ValidatorKeyAuthorization>> {
        let authorizations = self.authorizations.read().await;
        Ok(authorizations.values().cloned().collect())
    }

    /// Check if a validator is authorized for a specific node
    pub async fn is_validator_authorized(&self, validator_hotkey: &str, node_id: &str) -> bool {
        let authorizations = self.authorizations.read().await;
        if let Some(auth) = authorizations.get(validator_hotkey) {
            !auth.is_expired() && auth.authorized_nodes.contains(&node_id.to_string())
        } else {
            false
        }
    }

    /// Clean up expired authorizations
    pub async fn cleanup_expired(&self) -> Result<usize> {
        let mut expired_count = 0;

        let expired_validators: Vec<String> = {
            let authorizations = self.authorizations.read().await;
            authorizations
                .iter()
                .filter(|(_, auth)| auth.is_expired())
                .map(|(k, _)| k.clone())
                .collect()
        };

        for validator_hotkey in expired_validators {
            if let Err(e) = self.revoke_validator_key(&validator_hotkey, None).await {
                warn!(
                    "Failed to revoke expired authorization for {}: {}",
                    validator_hotkey, e
                );
            } else {
                expired_count += 1;
            }
        }

        if expired_count > 0 {
            info!("Cleaned up {} expired SSH authorizations", expired_count);
        }

        Ok(expired_count)
    }

    /// Get authorization statistics
    pub async fn get_authorization_stats(&self) -> Result<AuthorizationStats> {
        let authorizations = self.authorizations.read().await;

        let total_authorizations = authorizations.len();
        let expired_authorizations = authorizations.values().filter(|a| a.is_expired()).count();
        let active_authorizations = total_authorizations - expired_authorizations;

        let mut validators_by_node: HashMap<String, usize> = HashMap::new();
        for auth in authorizations.values() {
            if !auth.is_expired() {
                for node_id in &auth.authorized_nodes {
                    *validators_by_node.entry(node_id.clone()).or_insert(0) += 1;
                }
            }
        }

        Ok(AuthorizationStats {
            total_authorizations,
            active_authorizations,
            expired_authorizations,
            validators_by_node,
        })
    }

    /// Validate SSH public key format
    fn is_valid_ssh_public_key(&self, public_key: &str) -> bool {
        // Basic validation - check if it starts with known SSH key types
        public_key.starts_with("ssh-rsa ")
            || public_key.starts_with("ssh-ed25519 ")
            || public_key.starts_with("ecdsa-sha2-")
            || public_key.starts_with("ssh-dss ")
    }
}

/// Statistics about SSH authorizations
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthorizationStats {
    pub total_authorizations: usize,
    pub active_authorizations: usize,
    pub expired_authorizations: usize,
    pub validators_by_node: HashMap<String, usize>,
}

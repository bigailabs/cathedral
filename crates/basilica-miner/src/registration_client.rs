//! Miner Registration Client
//!
//! Low-level gRPC client for miner→validator communication:
//! - RegisterBid: Register nodes with SSH details + pricing
//! - HealthCheck: Periodic heartbeat to keep registrations active
//! - UpdateBid: Update pricing when it changes
//! - RemoveBid: Unregister nodes on shutdown
//! - SSH key deployment to nodes
//!
//! Note: The registration lifecycle (registration, health checks, price updates)
//! is orchestrated by AutoBidder. This module provides the underlying gRPC calls.

use std::sync::Arc;

use anyhow::{Context, Result};
use basilica_protocol::miner_discovery::{
    miner_registration_client::MinerRegistrationClient, HealthCheckRequest, NodeRegistration,
    RegisterBidRequest, RemoveBidRequest, UpdateBidRequest,
};
use chrono::Utc;
use tokio::sync::RwLock;
use tonic::transport::Channel;
use tracing::{debug, info, warn};

use crate::config::ValidatorCommsConfig;
use crate::node_manager::NodeManager;

/// Trait for Bittensor service operations needed for registration
pub trait BittensorServiceApi: Send + Sync {
    fn get_account_id(&self) -> String;
    fn sign_data(&self, data: &[u8]) -> Result<String>;
}

impl BittensorServiceApi for bittensor::Service {
    fn get_account_id(&self) -> String {
        self.get_account_id().to_string()
    }

    fn sign_data(&self, data: &[u8]) -> Result<String> {
        self.sign_data(data).map_err(|e| anyhow::anyhow!(e))
    }
}

/// Registration state tracking
#[derive(Debug, Clone)]
pub struct RegistrationState {
    /// Whether registration was successful
    pub registered: bool,
    /// Registration ID from validator
    pub registration_id: Option<String>,
    /// Validator's SSH public key to deploy
    pub validator_ssh_public_key: Option<String>,
    /// Health check interval from validator
    pub health_check_interval_secs: u32,
}

impl Default for RegistrationState {
    fn default() -> Self {
        Self {
            registered: false,
            registration_id: None,
            validator_ssh_public_key: None,
            health_check_interval_secs: 60,
        }
    }
}

/// Miner Registration Client
pub struct RegistrationClient {
    config: ValidatorCommsConfig,
    node_manager: Arc<NodeManager>,
    bittensor_service: Arc<dyn BittensorServiceApi>,
    miner_hotkey: String,
    state: Arc<RwLock<RegistrationState>>,
}

impl RegistrationClient {
    pub fn new(
        config: ValidatorCommsConfig,
        node_manager: Arc<NodeManager>,
        bittensor_service: Arc<dyn BittensorServiceApi>,
    ) -> Self {
        let miner_hotkey = bittensor_service.get_account_id();
        Self {
            config,
            node_manager,
            bittensor_service,
            miner_hotkey,
            state: Arc::new(RwLock::new(RegistrationState::default())),
        }
    }

    /// Check if registration endpoint is configured
    pub fn has_registration_endpoint(&self) -> bool {
        self.config.validator_registration_endpoint.is_some()
    }

    /// Get registration endpoint
    fn get_endpoint(&self) -> Option<&String> {
        self.config.validator_registration_endpoint.as_ref()
    }

    /// Connect to the validator's registration service
    async fn connect(&self) -> Result<MinerRegistrationClient<Channel>> {
        let endpoint = self
            .get_endpoint()
            .ok_or_else(|| anyhow::anyhow!("validator_registration_endpoint not configured"))?;

        let channel = Channel::from_shared(endpoint.clone())
            .context("invalid endpoint URL")?
            .timeout(self.config.request_timeout)
            .connect()
            .await
            .context("failed to connect to validator")?;

        Ok(MinerRegistrationClient::new(channel))
    }

    /// Build and sign the message for RegisterBid
    fn build_register_bid_message(&self, timestamp: i64, nonce: &str) -> String {
        format!(
            "{}|{}|{}",
            self.miner_hotkey.trim(),
            timestamp,
            nonce.trim()
        )
    }

    /// Build and sign the message for UpdateBid
    fn build_update_bid_message(
        &self,
        node_id: &str,
        hourly_rate_cents: u32,
        timestamp: i64,
        nonce: &str,
    ) -> String {
        format!(
            "{}|{}|{}|{}|{}",
            self.miner_hotkey.trim(),
            node_id.trim(),
            hourly_rate_cents,
            timestamp,
            nonce.trim()
        )
    }

    /// Build and sign the message for RemoveBid
    fn build_remove_bid_message(&self, node_ids: &[String], timestamp: i64, nonce: &str) -> String {
        let node_ids_str = node_ids.join(",");
        format!(
            "{}|{}|{}|{}",
            self.miner_hotkey.trim(),
            node_ids_str,
            timestamp,
            nonce.trim()
        )
    }

    /// Build and sign the message for HealthCheck
    fn build_health_check_message(&self, timestamp: i64) -> String {
        format!("{}|{}", self.miner_hotkey.trim(), timestamp)
    }

    /// Sign a message using the miner's hotkey
    fn sign_message(&self, message: &str) -> Result<Vec<u8>> {
        let signature_hex = self
            .bittensor_service
            .sign_data(message.as_bytes())
            .context("failed to sign message")?;
        let signature_bytes =
            hex::decode(signature_hex).context("failed to decode signature hex")?;
        Ok(signature_bytes)
    }

    /// Generate a nonce for replay protection
    fn generate_nonce() -> String {
        uuid::Uuid::new_v4().to_string()
    }

    /// Register nodes with the validator using pre-built registrations.
    /// This is the primary registration method - AutoBidder builds the registrations
    /// with prices from BiddingConfig.
    pub async fn register_nodes_with_registrations(
        &self,
        node_registrations: Vec<NodeRegistration>,
    ) -> Result<RegistrationState> {
        if !self.has_registration_endpoint() {
            return Err(anyhow::anyhow!(
                "validator_registration_endpoint not configured"
            ));
        }

        if node_registrations.is_empty() {
            warn!("No nodes to register");
            return Ok(RegistrationState::default());
        }

        let mut client = self.connect().await?;

        // Build and sign request
        let timestamp = Utc::now().timestamp();
        let nonce = Self::generate_nonce();
        let message = self.build_register_bid_message(timestamp, &nonce);
        let signature = self.sign_message(&message)?;

        let node_count = node_registrations.len();
        let request = RegisterBidRequest {
            miner_hotkey: self.miner_hotkey.clone(),
            nodes: node_registrations,
            timestamp,
            nonce,
            signature,
        };

        info!(
            miner_hotkey = %self.miner_hotkey,
            node_count = node_count,
            "Registering nodes with validator"
        );

        let response = client
            .register_bid(request)
            .await
            .context("RegisterBid RPC failed")?
            .into_inner();

        if !response.accepted {
            return Err(anyhow::anyhow!(
                "Registration rejected: {}",
                response.error_message
            ));
        }

        let state = RegistrationState {
            registered: true,
            registration_id: Some(response.registration_id.clone()),
            validator_ssh_public_key: if response.validator_ssh_public_key.is_empty() {
                None
            } else {
                Some(response.validator_ssh_public_key.clone())
            },
            health_check_interval_secs: response.health_check_interval_secs,
        };

        // Update internal state
        *self.state.write().await = state.clone();

        info!(
            registration_id = %response.registration_id,
            health_check_interval_secs = response.health_check_interval_secs,
            has_ssh_key = !response.validator_ssh_public_key.is_empty(),
            "Successfully registered with validator"
        );

        // Log collateral status if present
        if let Some(status) = &response.collateral_status {
            match status.status.as_str() {
                "warning" | "undercollateralized" | "excluded" => {
                    warn!(
                        status = %status.status,
                        current_usd = status.current_usd_value,
                        min_required = status.minimum_usd_required,
                        action = %status.action_required,
                        "Collateral status requires attention"
                    );
                }
                _ => {
                    info!(
                        status = %status.status,
                        current_usd = status.current_usd_value,
                        "Collateral status OK"
                    );
                }
            }
        }

        Ok(state)
    }

    /// Deploy validator's SSH key to all nodes.
    /// Called after successful registration.
    pub async fn deploy_validator_ssh_key(&self) -> Result<()> {
        let state = self.state.read().await;
        let ssh_key = match &state.validator_ssh_public_key {
            Some(key) if !key.is_empty() => key.clone(),
            _ => {
                info!("No validator SSH key to deploy");
                return Ok(());
            }
        };
        drop(state); // Release lock before async operation

        info!("Deploying validator SSH key to nodes");

        // Use node manager to deploy SSH keys
        // Note: This assumes node_manager has a method to deploy SSH keys
        // The validator_hotkey is used as identifier for the key
        self.node_manager
            .deploy_validator_keys("validator", &ssh_key)
            .await
            .context("failed to deploy validator SSH key to nodes")?;

        info!("Successfully deployed validator SSH key to all nodes");
        Ok(())
    }

    /// Send periodic health check to validator.
    pub async fn send_health_check(&self) -> Result<u32> {
        let state = self.state.read().await;
        if !state.registered {
            return Err(anyhow::anyhow!("not registered yet"));
        }
        drop(state);

        let mut client = self.connect().await?;

        let timestamp = Utc::now().timestamp();
        let message = self.build_health_check_message(timestamp);
        let signature = self.sign_message(&message)?;

        let request = HealthCheckRequest {
            miner_hotkey: self.miner_hotkey.clone(),
            node_ids: vec![], // Empty = all nodes
            timestamp,
            signature,
        };

        let response = client
            .health_check(request)
            .await
            .context("HealthCheck RPC failed")?
            .into_inner();

        if !response.accepted {
            return Err(anyhow::anyhow!(
                "Health check rejected: {}",
                response.error_message
            ));
        }

        debug!(
            nodes_active = response.nodes_active,
            "Health check successful"
        );
        Ok(response.nodes_active)
    }

    /// Update price for a specific node.
    pub async fn update_node_price(&self, node_id: &str, hourly_rate_cents: u32) -> Result<()> {
        let state = self.state.read().await;
        if !state.registered {
            return Err(anyhow::anyhow!("not registered yet"));
        }
        drop(state);

        let mut client = self.connect().await?;

        let timestamp = Utc::now().timestamp();
        let nonce = Self::generate_nonce();
        let message = self.build_update_bid_message(node_id, hourly_rate_cents, timestamp, &nonce);
        let signature = self.sign_message(&message)?;

        let request = UpdateBidRequest {
            miner_hotkey: self.miner_hotkey.clone(),
            node_id: node_id.to_string(),
            hourly_rate_cents,
            timestamp,
            nonce,
            signature,
        };

        let response = client
            .update_bid(request)
            .await
            .context("UpdateBid RPC failed")?
            .into_inner();

        if !response.accepted {
            return Err(anyhow::anyhow!(
                "Price update rejected: {}",
                response.error_message
            ));
        }

        info!(
            node_id = node_id,
            hourly_rate_cents = hourly_rate_cents,
            "Updated node price"
        );
        Ok(())
    }

    /// Remove nodes from availability.
    /// If node_ids is empty, removes all nodes.
    pub async fn remove_nodes(&self, node_ids: Vec<String>) -> Result<u32> {
        let state = self.state.read().await;
        if !state.registered {
            return Err(anyhow::anyhow!("not registered yet"));
        }
        drop(state);

        let mut client = self.connect().await?;

        let timestamp = Utc::now().timestamp();
        let nonce = Self::generate_nonce();
        let message = self.build_remove_bid_message(&node_ids, timestamp, &nonce);
        let signature = self.sign_message(&message)?;

        let request = RemoveBidRequest {
            miner_hotkey: self.miner_hotkey.clone(),
            node_ids,
            timestamp,
            nonce,
            signature,
        };

        let response = client
            .remove_bid(request)
            .await
            .context("RemoveBid RPC failed")?
            .into_inner();

        if !response.accepted {
            return Err(anyhow::anyhow!(
                "Node removal rejected: {}",
                response.error_message
            ));
        }

        info!(nodes_removed = response.nodes_removed, "Removed nodes");
        Ok(response.nodes_removed)
    }

    /// Get current registration state
    pub async fn get_state(&self) -> RegistrationState {
        self.state.read().await.clone()
    }
}
#[cfg(test)]
mod tests {
    use super::*;

    struct MockBittensorService {
        hotkey: String,
    }

    impl BittensorServiceApi for MockBittensorService {
        fn get_account_id(&self) -> String {
            self.hotkey.clone()
        }

        fn sign_data(&self, _data: &[u8]) -> Result<String> {
            // Return a fake signature for testing
            Ok("aabbccdd".to_string())
        }
    }

    #[test]
    fn test_build_register_bid_message() {
        let config = ValidatorCommsConfig::default();
        let node_manager = Arc::new(NodeManager::new(crate::config::NodeSshConfig::default()));
        let bittensor = Arc::new(MockBittensorService {
            hotkey: "5GrwvaEF".to_string(),
        });

        let client = RegistrationClient::new(config, node_manager, bittensor);
        let message = client.build_register_bid_message(1234567890, "nonce123");
        assert_eq!(message, "5GrwvaEF|1234567890|nonce123");
    }

    #[test]
    fn test_build_health_check_message() {
        let config = ValidatorCommsConfig::default();
        let node_manager = Arc::new(NodeManager::new(crate::config::NodeSshConfig::default()));
        let bittensor = Arc::new(MockBittensorService {
            hotkey: "5GrwvaEF".to_string(),
        });

        let client = RegistrationClient::new(config, node_manager, bittensor);
        let message = client.build_health_check_message(1234567890);
        assert_eq!(message, "5GrwvaEF|1234567890");
    }

    #[test]
    fn test_build_update_bid_message() {
        let config = ValidatorCommsConfig::default();
        let node_manager = Arc::new(NodeManager::new(crate::config::NodeSshConfig::default()));
        let bittensor = Arc::new(MockBittensorService {
            hotkey: "5GrwvaEF".to_string(),
        });

        let client = RegistrationClient::new(config, node_manager, bittensor);
        let message = client.build_update_bid_message("node-1", 250, 1234567890, "nonce123");
        assert_eq!(message, "5GrwvaEF|node-1|250|1234567890|nonce123");
    }

    #[test]
    fn test_build_remove_bid_message() {
        let config = ValidatorCommsConfig::default();
        let node_manager = Arc::new(NodeManager::new(crate::config::NodeSshConfig::default()));
        let bittensor = Arc::new(MockBittensorService {
            hotkey: "5GrwvaEF".to_string(),
        });

        let client = RegistrationClient::new(config, node_manager, bittensor);
        let message = client.build_remove_bid_message(
            &["node-1".to_string(), "node-2".to_string()],
            1234567890,
            "nonce123",
        );
        assert_eq!(message, "5GrwvaEF|node-1,node-2|1234567890|nonce123");
    }
}

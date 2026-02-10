//! Validator discovery and assignment module
//!
//! This module handles discovering active validators and selecting the single
//! validator that should receive all managed nodes. After selecting a validator,
//! it calls the validator's `/discovery` HTTP endpoint to learn the gRPC port
//! for bid registration.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{debug, error, info, warn};

use crate::node_manager::{NodeManager, RegisteredNode};

/// Information about a validator
#[derive(Debug, Clone)]
pub struct ValidatorInfo {
    pub uid: u16,
    pub hotkey: String,
    pub coldkey: String,
    pub stake: u64,
    /// Axon endpoint from metagraph (e.g., "http://1.2.3.4:8080")
    pub axon_endpoint: Option<String>,
}

/// A fully discovered validator with a known gRPC endpoint
#[derive(Debug, Clone)]
pub struct DiscoveredValidator {
    pub hotkey: String,
    pub grpc_endpoint: String,
}

/// Response from the validator's /discovery endpoint
#[derive(Debug, serde::Deserialize)]
struct DiscoveryResponse {
    bid_grpc_port: u16,
    #[allow(dead_code)]
    version: Option<String>,
}

/// Validator discovery service
pub struct ValidatorDiscovery {
    bittensor_service: Arc<bittensor::Service>,
    node_manager: Arc<NodeManager>,
    assignment_strategy: Box<dyn AssignmentStrategy>,
    netuid: u16,
    discovered: RwLock<Option<DiscoveredValidator>>,
}

/// Strategy for assigning nodes to validators
#[async_trait]
pub trait AssignmentStrategy: Send + Sync {
    /// Select validator to assign nodes to (all nodes are assigned to the selected validator)
    async fn select_assignment(
        &self,
        validators: Vec<ValidatorInfo>,
        nodes: Vec<RegisteredNode>,
    ) -> Result<Option<ValidatorInfo>>;
}

impl ValidatorDiscovery {
    /// Create new validator discovery service
    pub fn new(
        bittensor_service: Arc<bittensor::Service>,
        node_manager: Arc<NodeManager>,
        assignment_strategy: Box<dyn AssignmentStrategy>,
        netuid: u16,
    ) -> Self {
        Self {
            bittensor_service,
            node_manager,
            assignment_strategy,
            netuid,
            discovered: RwLock::new(None),
        }
    }

    /// Get list of active validators from the metagraph
    pub async fn get_active_validators(&self) -> Result<Vec<ValidatorInfo>> {
        let metagraph = self.bittensor_service.get_metagraph(self.netuid).await?;
        let discovery = bittensor::NeuronDiscovery::new(&metagraph);

        let neurons = discovery.get_validators()?;
        let validators: Vec<ValidatorInfo> = neurons
            .into_iter()
            .map(|n| {
                let axon_endpoint = n.axon_info.map(|a| format!("http://{}:{}", a.ip, a.port));

                ValidatorInfo {
                    uid: n.uid,
                    hotkey: n.hotkey,
                    coldkey: n.coldkey,
                    stake: n.stake,
                    axon_endpoint,
                }
            })
            .collect();

        debug!("Found {} active validators", validators.len());
        Ok(validators)
    }

    /// Perform discovery and assignment
    pub async fn run_discovery(&self) -> Result<()> {
        info!("Starting validator discovery");

        // Get active validators
        let validators = self.get_active_validators().await?;
        info!("Found {} validators with permits", validators.len());

        // Get available nodes
        let nodes = self.node_manager.list_nodes().await?;
        info!("Found {} available nodes", nodes.len());

        if nodes.is_empty() {
            warn!("No nodes available for assignment");
            return Ok(());
        }

        // Run assignment strategy to select the validator (all nodes go to selected validator)
        let selected_validator = self
            .assignment_strategy
            .select_assignment(validators, nodes)
            .await?;

        if let Some(validator) = selected_validator {
            info!(
                "Assigning all nodes to validator {} (uid: {})",
                validator.hotkey, validator.uid
            );

            // Update assignment in NodeManager (single source of truth)
            self.node_manager
                .set_assigned_validator(&validator.hotkey)
                .await;

            // Call the validator's /discovery endpoint to learn the gRPC port
            if let Some(ref axon_endpoint) = validator.axon_endpoint {
                match self
                    .call_discovery_endpoint(axon_endpoint, &validator.hotkey)
                    .await
                {
                    Ok(discovered) => {
                        info!(
                            grpc_endpoint = %discovered.grpc_endpoint,
                            "Discovered validator gRPC endpoint"
                        );
                        *self.discovered.write().await = Some(discovered);
                    }
                    Err(e) => {
                        error!(
                            "Failed to call /discovery on validator {}: {}",
                            validator.hotkey, e
                        );
                    }
                }
            } else {
                warn!(
                    "Validator {} has no axon endpoint; cannot discover gRPC port",
                    validator.hotkey
                );
            }
        } else {
            info!("No eligible validators found during discovery; no assignment made");
        }

        Ok(())
    }

    /// Call the validator's /discovery endpoint and construct the full gRPC URL.
    async fn call_discovery_endpoint(
        &self,
        axon_endpoint: &str,
        hotkey: &str,
    ) -> Result<DiscoveredValidator> {
        let discovery_url = format!("{}/discovery", axon_endpoint);
        debug!("Calling discovery endpoint: {}", discovery_url);

        let resp: DiscoveryResponse = reqwest::get(&discovery_url)
            .await
            .map_err(|e| anyhow::anyhow!("HTTP request to {} failed: {}", discovery_url, e))?
            .error_for_status()
            .map_err(|e| anyhow::anyhow!("Discovery endpoint returned error: {}", e))?
            .json()
            .await
            .map_err(|e| anyhow::anyhow!("Failed to parse discovery response: {}", e))?;

        // Extract the host/IP from the axon endpoint
        let axon_url = url::Url::parse(axon_endpoint)
            .map_err(|e| anyhow::anyhow!("Invalid axon endpoint URL: {}", e))?;
        let host = axon_url
            .host_str()
            .ok_or_else(|| anyhow::anyhow!("No host in axon endpoint"))?;

        let grpc_endpoint = format!("http://{}:{}", host, resp.bid_grpc_port);

        Ok(DiscoveredValidator {
            hotkey: hotkey.to_string(),
            grpc_endpoint,
        })
    }

    /// Get the currently discovered validator (if any).
    pub async fn get_discovered_validator(&self) -> Option<DiscoveredValidator> {
        self.discovered.read().await.clone()
    }
}

/// Fixed assignment strategy - assigns all nodes to a specific validator
pub struct FixedAssignment {
    validator_hotkey: String,
}

impl FixedAssignment {
    pub fn new(validator_hotkey: String) -> Self {
        Self { validator_hotkey }
    }
}

#[async_trait]
impl AssignmentStrategy for FixedAssignment {
    async fn select_assignment(
        &self,
        validators: Vec<ValidatorInfo>,
        _nodes: Vec<RegisteredNode>,
    ) -> Result<Option<ValidatorInfo>> {
        // Find the validator with the specified hotkey
        if let Some(validator) = validators
            .into_iter()
            .find(|v| v.hotkey == self.validator_hotkey)
        {
            Ok(Some(validator))
        } else {
            warn!(
                "Validator with hotkey {} not found in active validators",
                self.validator_hotkey
            );
            Ok(None)
        }
    }
}

/// Highest stake assignment strategy - assigns all nodes to the validator with highest stake
pub struct HighestStakeAssignment;

#[async_trait]
impl AssignmentStrategy for HighestStakeAssignment {
    async fn select_assignment(
        &self,
        mut validators: Vec<ValidatorInfo>,
        _nodes: Vec<RegisteredNode>,
    ) -> Result<Option<ValidatorInfo>> {
        // Sort by stake (highest first)
        validators.sort_by(|a, b| b.stake.cmp(&a.stake));

        // Take the highest staked validator
        Ok(validators.into_iter().next())
    }
}

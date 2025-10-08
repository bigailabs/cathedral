//! Validator discovery and assignment module
//!
//! This module handles discovering active validators and selecting the single
//! validator that should receive all managed nodes.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{debug, info, warn};

use crate::node_manager::{NodeManager, RegisteredNode};

/// Information about a validator
#[derive(Debug, Clone)]
pub struct ValidatorInfo {
    pub uid: u16,
    pub hotkey: String,
    pub coldkey: String,
    pub stake: u128,
    pub trust: f64,
    pub consensus: f64,
    pub validator_permit: bool,
}

/// Validator discovery service
pub struct ValidatorDiscovery {
    bittensor_service: Arc<bittensor::Service>,
    node_manager: Arc<NodeManager>,
    assignment_strategy: Box<dyn AssignmentStrategy>,
    current_assignment: Arc<RwLock<Option<ActiveAssignment>>>,
    netuid: u16,
}

/// Active validator assignment for this miner
#[derive(Clone, Debug)]
pub struct ActiveAssignment {
    pub validator_hotkey: String,
    pub node_ids: Vec<String>,
}

/// Strategy for assigning nodes to validators
#[async_trait]
pub trait AssignmentStrategy: Send + Sync {
    /// Select validator to assign nodes to
    async fn select_assignment(
        &self,
        validators: Vec<ValidatorInfo>,
        nodes: Vec<RegisteredNode>,
    ) -> Result<Option<(ValidatorInfo, Vec<RegisteredNode>)>>;
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
            current_assignment: Arc::new(RwLock::new(None)),
            netuid,
        }
    }

    /// Get list of active validators from the metagraph
    pub async fn get_active_validators(&self) -> Result<Vec<ValidatorInfo>> {
        let metagraph = self.bittensor_service.get_metagraph(self.netuid).await?;

        let validators: Vec<ValidatorInfo> = (0..metagraph.hotkeys.len())
            .filter_map(|idx| {
                let uid = idx as u16;
                let is_validator = metagraph
                    .validator_permit
                    .get(idx)
                    .copied()
                    .unwrap_or(false);

                if is_validator {
                    let hotkey = metagraph.hotkeys.get(idx)?.to_string();
                    let coldkey = metagraph
                        .coldkeys
                        .get(idx)
                        .map(|c| c.to_string())
                        .unwrap_or_default();
                    let stake = metagraph
                        .total_stake
                        .get(idx)
                        .map(|s| s.0 as u128)
                        .unwrap_or(0);
                    let trust = metagraph
                        .trust
                        .get(idx)
                        .map(|t| t.0 as f64 / 65535.0)
                        .unwrap_or(0.0);
                    let consensus = metagraph
                        .consensus
                        .get(idx)
                        .map(|c| c.0 as f64 / 65535.0)
                        .unwrap_or(0.0);

                    Some(ValidatorInfo {
                        uid,
                        hotkey,
                        coldkey,
                        stake,
                        trust,
                        consensus,
                        validator_permit: true,
                    })
                } else {
                    None
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

        // Run assignment strategy and capture first assignment (single-validator model)
        let selected_assignment = self
            .assignment_strategy
            .select_assignment(validators, nodes)
            .await?;

        let mut current_assignment = self.current_assignment.write().await;

        if let Some((validator, assigned_nodes)) = selected_assignment {
            let node_ids: Vec<String> = assigned_nodes
                .iter()
                .map(|node| node.node_id.clone())
                .collect();

            info!(
                "Assigned {} nodes to validator {} (uid: {})",
                node_ids.len(),
                validator.hotkey,
                validator.uid
            );

            *current_assignment = Some(ActiveAssignment {
                validator_hotkey: validator.hotkey,
                node_ids,
            });
        } else {
            info!("No eligible validators found during discovery; clearing assignment");
            current_assignment.take();
        }

        Ok(())
    }

    /// Get the currently assigned validator and nodes, if any
    pub async fn get_current_assignment(&self) -> Option<ActiveAssignment> {
        self.current_assignment.read().await.clone()
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
        nodes: Vec<RegisteredNode>,
    ) -> Result<Option<(ValidatorInfo, Vec<RegisteredNode>)>> {
        // Find the validator with the specified hotkey
        if let Some(validator) = validators
            .into_iter()
            .find(|v| v.hotkey == self.validator_hotkey)
        {
            Ok(Some((validator, nodes)))
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
        nodes: Vec<RegisteredNode>,
    ) -> Result<Option<(ValidatorInfo, Vec<RegisteredNode>)>> {
        // Sort by stake (highest first)
        validators.sort_by(|a, b| b.stake.cmp(&a.stake));

        // Take the highest staked validator
        if let Some(validator) = validators.into_iter().next() {
            Ok(Some((validator, nodes)))
        } else {
            Ok(None)
        }
    }
}

//! Validator discovery and assignment module
//!
//! This module handles discovering active validators and assigning nodes to them.

use anyhow::Result;
use async_trait::async_trait;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{debug, info, warn};

use crate::node_manager::{NodeConfig, NodeManager};
use sqlx::SqlitePool;

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
    assignments: Arc<RwLock<HashMap<String, Vec<String>>>>, // validator_hotkey -> node_ids
    netuid: u16,
}

/// Strategy for assigning nodes to validators
#[async_trait]
pub trait AssignmentStrategy: Send + Sync {
    /// Select validators to assign nodes to
    async fn select_validators(
        &self,
        validators: Vec<ValidatorInfo>,
        nodes: Vec<NodeConfig>,
    ) -> Result<Vec<(ValidatorInfo, Vec<NodeConfig>)>>;
}

/// Round-robin assignment strategy
pub struct RoundRobinAssignment;

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
            assignments: Arc::new(RwLock::new(HashMap::new())),
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

        // Run assignment strategy
        let assignments = self
            .assignment_strategy
            .select_validators(validators, nodes)
            .await?;

        // Update assignments
        let mut current_assignments = self.assignments.write().await;
        current_assignments.clear();

        for (validator, assigned_nodes) in assignments {
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

            current_assignments.insert(validator.hotkey, node_ids);
        }

        Ok(())
    }

    /// Get current assignments
    pub async fn get_assignments(&self) -> HashMap<String, Vec<String>> {
        self.assignments.read().await.clone()
    }

    /// Get nodes assigned to a specific validator
    pub async fn get_validator_nodes(&self, validator_hotkey: &str) -> Vec<String> {
        self.assignments
            .read()
            .await
            .get(validator_hotkey)
            .cloned()
            .unwrap_or_default()
    }
}

#[async_trait]
impl AssignmentStrategy for RoundRobinAssignment {
    async fn select_validators(
        &self,
        validators: Vec<ValidatorInfo>,
        nodes: Vec<NodeConfig>,
    ) -> Result<Vec<(ValidatorInfo, Vec<NodeConfig>)>> {
        if validators.is_empty() || nodes.is_empty() {
            return Ok(vec![]);
        }

        let mut assignments = Vec::new();
        let nodes_per_validator = (nodes.len() / validators.len()).max(1);
        let mut node_iter = nodes.into_iter();

        for validator in validators {
            let mut validator_nodes = Vec::new();
            for _ in 0..nodes_per_validator {
                if let Some(node) = node_iter.next() {
                    validator_nodes.push(node);
                } else {
                    break;
                }
            }

            if !validator_nodes.is_empty() {
                assignments.push((validator, validator_nodes));
            }
        }

        // Assign remaining nodes to the first validator
        if let Some(remaining) = node_iter.next() {
            if let Some((_, ref mut nodes)) = assignments.first_mut() {
                nodes.push(remaining);
                for node in node_iter {
                    nodes.push(node);
                }
            }
        }

        Ok(assignments)
    }
}

/// Highest stake assignment strategy
pub struct HighestStakeAssignment {
    _pool: SqlitePool,
    min_stake_threshold: u128,
    validator_hotkey: Option<String>,
}

impl HighestStakeAssignment {
    pub fn new(
        pool: SqlitePool,
        min_stake_threshold: u128,
        validator_hotkey: Option<String>,
    ) -> Self {
        Self {
            _pool: pool,
            min_stake_threshold,
            validator_hotkey,
        }
    }
}

#[async_trait]
impl AssignmentStrategy for HighestStakeAssignment {
    async fn select_validators(
        &self,
        mut validators: Vec<ValidatorInfo>,
        nodes: Vec<NodeConfig>,
    ) -> Result<Vec<(ValidatorInfo, Vec<NodeConfig>)>> {
        // Filter by minimum stake
        validators.retain(|v| v.stake >= self.min_stake_threshold);

        // If specific validator is configured, filter to just that one
        if let Some(ref hotkey) = self.validator_hotkey {
            validators.retain(|v| &v.hotkey == hotkey);
        }

        // Sort by stake (highest first)
        validators.sort_by(|a, b| b.stake.cmp(&a.stake));

        // Take the highest staked validator
        if let Some(validator) = validators.into_iter().next() {
            Ok(vec![(validator, nodes)])
        } else {
            Ok(vec![])
        }
    }
}

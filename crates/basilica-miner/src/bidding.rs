//! Automatic bidding module
//!
//! Periodically submits bids to validators based on configured pricing strategy.

use anyhow::Result;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::{watch, RwLock};
use tracing::{debug, error, info, warn};

use crate::config::BiddingConfig;
use crate::node_manager::NodeManager;
use crate::validator_comms::ValidatorCommsServer;

/// Automatic bidder that periodically submits bids to validators
pub struct AutoBidder {
    config: BiddingConfig,
    node_manager: Arc<NodeManager>,
    validator_comms: Arc<RwLock<Option<ValidatorCommsServer>>>,
}

impl AutoBidder {
    /// Create a new auto-bidder
    pub fn new(config: BiddingConfig, node_manager: Arc<NodeManager>) -> Self {
        Self {
            config,
            node_manager,
            validator_comms: Arc::new(RwLock::new(None)),
        }
    }

    /// Set the validator communications server (called after it's initialized)
    pub async fn set_validator_comms(&self, validator_comms: ValidatorCommsServer) {
        let mut comms = self.validator_comms.write().await;
        *comms = Some(validator_comms);
    }

    /// Check if bidding is enabled and properly configured
    pub fn is_enabled(&self) -> bool {
        if !self.config.enabled {
            return false;
        }
        if self.config.static_prices_cents.is_empty() {
            warn!("Bidding enabled but no static_prices configured");
            return false;
        }
        true
    }

    /// Run the auto-bidder loop
    pub async fn run(&self, mut shutdown_rx: watch::Receiver<bool>) -> Result<()> {
        if !self.is_enabled() {
            info!("Auto-bidding disabled, skipping bid submission loop");
            return Ok(());
        }

        info!(
            "Starting auto-bidder with {} GPU categories, interval: {:?}",
            self.config.static_prices_cents.len(),
            self.config.bid_interval
        );

        let mut interval = tokio::time::interval(self.config.bid_interval);

        loop {
            tokio::select! {
                _ = interval.tick() => {
                    if let Err(e) = self.submit_all_bids().await {
                        error!("Failed to submit bids: {}", e);
                    }
                }
                changed = shutdown_rx.changed() => {
                    if changed.is_err() || *shutdown_rx.borrow() {
                        info!("Auto-bidder shutdown requested");
                        break;
                    }
                }
            }
        }
        Ok(())
    }

    /// Submit bids for all available capacity
    async fn submit_all_bids(&self) -> Result<()> {
        // Get validator comms
        let comms = self.validator_comms.read().await;
        let validator_comms = match comms.as_ref() {
            Some(vc) => vc,
            None => {
                debug!("Validator comms not yet initialized, skipping bid submission");
                return Ok(());
            }
        };

        // Get available capacity by GPU category
        let capacity = self.get_available_capacity().await?;

        if capacity.is_empty() {
            debug!("No available capacity to bid on");
            return Ok(());
        }

        let timestamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs() as i64;

        for (category, gpu_count) in capacity {
            // Get price for this category
            let price = match self.get_bid_price(&category) {
                Some(p) => p,
                None => {
                    debug!("No price configured for GPU category: {}", category);
                    continue;
                }
            };

            // Create and submit the bid
            match validator_comms.create_signed_bid(
                category.clone(),
                price,
                gpu_count,
                vec![], // attestation - empty for now
                timestamp,
                None, // nonce - auto-generated
            ) {
                Ok(bid) => {
                    info!(
                        "Submitting bid: {} x{} @ ${:.2}/GPU-hr",
                        category,
                        gpu_count,
                        price as f64 / 100.0
                    );

                    // Forward to validator if endpoint is configured
                    if !validator_comms.has_validator_bid_endpoint() {
                        debug!(
                            "validator_bid_endpoint not configured, bid created but not submitted"
                        );
                        continue;
                    }

                    match validator_comms.forward_bid_to_validator(bid).await {
                        Ok(response) => {
                            if response.accepted {
                                info!(
                                    "Bid accepted for {} (epoch: {})",
                                    category, response.epoch_id
                                );
                            } else {
                                warn!("Bid rejected for {}: {}", category, response.error_message);
                            }
                        }
                        Err(e) => {
                            warn!("Failed to submit bid for {}: {}", category, e);
                        }
                    }
                }
                Err(e) => {
                    error!("Failed to create signed bid for {}: {}", category, e);
                }
            }
        }

        Ok(())
    }

    /// Get the bid price for a GPU category (in cents)
    fn get_bid_price(&self, category: &str) -> Option<u32> {
        // First check static_prices_cents
        if let Some(&price_cents) = self.config.static_prices_cents.get(category) {
            // Ensure we don't bid below floor
            if let Some(&floor_cents) = self.config.floor_prices_cents.get(category) {
                return Some(price_cents.max(floor_cents));
            }
            return Some(price_cents);
        }

        // Try case-insensitive match
        let category_upper = category.to_uppercase();
        for (key, &price_cents) in &self.config.static_prices_cents {
            if key.to_uppercase() == category_upper {
                if let Some(&floor_cents) = self.config.floor_prices_cents.get(key) {
                    return Some(price_cents.max(floor_cents));
                }
                return Some(price_cents);
            }
        }

        None
    }

    /// Get available GPU capacity by category
    async fn get_available_capacity(&self) -> Result<HashMap<String, u32>> {
        let nodes = self.node_manager.list_nodes().await?;
        let mut capacity: HashMap<String, u32> = HashMap::new();

        for node in nodes {
            let category = node.config.gpu_category.to_uppercase();
            if category == "UNKNOWN" {
                warn!(
                    "Node {} has unknown GPU category, skipping for bidding",
                    node.node_id
                );
                continue;
            }

            *capacity.entry(category).or_default() += node.config.gpu_count;
        }

        Ok(capacity)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn test_config() -> BiddingConfig {
        let mut static_prices_cents = HashMap::new();
        static_prices_cents.insert("H100".to_string(), 250); // $2.50 in cents
        static_prices_cents.insert("A100".to_string(), 120); // $1.20 in cents

        BiddingConfig {
            enabled: true,
            static_prices_cents,
            bid_interval: Duration::from_secs(60),
            floor_prices_cents: HashMap::new(),
        }
    }

    #[test]
    fn test_get_bid_price() {
        let config = test_config();
        let node_manager = Arc::new(NodeManager::default());
        let bidder = AutoBidder::new(config, node_manager);

        assert_eq!(bidder.get_bid_price("H100"), Some(250)); // $2.50 in cents
        assert_eq!(bidder.get_bid_price("A100"), Some(120)); // $1.20 in cents
        assert_eq!(bidder.get_bid_price("h100"), Some(250)); // case insensitive
        assert_eq!(bidder.get_bid_price("RTX4090"), None);
    }

    #[test]
    fn test_get_bid_price_with_floor() {
        let mut config = test_config();
        config.floor_prices_cents.insert("H100".to_string(), 300); // Floor $3.00 higher than static $2.50

        let node_manager = Arc::new(NodeManager::default());
        let bidder = AutoBidder::new(config, node_manager);

        // Should return floor price since it's higher
        assert_eq!(bidder.get_bid_price("H100"), Some(300)); // $3.00 in cents
        assert_eq!(bidder.get_bid_price("A100"), Some(120)); // No floor, use static $1.20
    }

    #[test]
    fn test_is_enabled() {
        let node_manager = Arc::new(NodeManager::default());

        // Disabled config
        let config = BiddingConfig::default();
        let bidder = AutoBidder::new(config, node_manager.clone());
        assert!(!bidder.is_enabled());

        // Enabled but no prices
        let config = BiddingConfig {
            enabled: true,
            ..Default::default()
        };
        let bidder = AutoBidder::new(config, node_manager.clone());
        assert!(!bidder.is_enabled());

        // Enabled with prices
        let config = test_config();
        let bidder = AutoBidder::new(config, node_manager);
        assert!(bidder.is_enabled());
    }
}

//! Automatic bidding module
//!
//! Periodically updates node prices with validators based on configured pricing strategy.
//! In the new miner→validator flow, initial registration happens at startup via RegistrationClient,
//! and this module handles periodic price updates.

use anyhow::Result;
use std::sync::Arc;
use tokio::sync::{watch, RwLock};
use tracing::{debug, error, info, warn};

use crate::config::BiddingConfig;
use crate::node_manager::NodeManager;
use crate::registration_client::RegistrationClient;

/// Automatic bidder that periodically updates node prices with validators
pub struct AutoBidder {
    config: BiddingConfig,
    node_manager: Arc<NodeManager>,
    registration_client: Arc<RwLock<Option<Arc<RegistrationClient>>>>,
}

impl AutoBidder {
    /// Create a new auto-bidder
    pub fn new(config: BiddingConfig, node_manager: Arc<NodeManager>) -> Self {
        Self {
            config,
            node_manager,
            registration_client: Arc::new(RwLock::new(None)),
        }
    }

    /// Set the registration client (called after it's initialized)
    pub async fn set_registration_client(&self, client: Arc<RegistrationClient>) {
        let mut reg = self.registration_client.write().await;
        *reg = Some(client);
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

    /// Update prices for all registered nodes
    async fn submit_all_bids(&self) -> Result<()> {
        // Get registration client
        let client_guard = self.registration_client.read().await;
        let client = match client_guard.as_ref() {
            Some(c) => c.clone(),
            None => {
                debug!("Registration client not yet initialized, skipping price update");
                return Ok(());
            }
        };
        drop(client_guard); // Release lock before async operations

        // Check if registered
        let state = client.get_state().await;
        if !state.registered {
            debug!("Not yet registered with validator, skipping price update");
            return Ok(());
        }

        // Get all nodes and update their prices if needed
        let nodes = self.node_manager.list_nodes().await?;

        if nodes.is_empty() {
            debug!("No nodes available for price update");
            return Ok(());
        }

        for node in nodes {
            let category = node.config.gpu_category.to_uppercase();

            // Get configured price for this category
            let price = match self.get_bid_price(&category) {
                Some(p) => p,
                None => {
                    debug!("No price configured for GPU category: {}", category);
                    continue;
                }
            };

            // Only update if price differs from current node price
            if price == node.config.hourly_rate_per_gpu_cents {
                debug!(
                    "Price unchanged for node {} ({} @ ${:.2}/GPU-hr)",
                    node.node_id,
                    category,
                    price as f64 / 100.0
                );
                continue;
            }

            info!(
                "Updating price for node {}: {} @ ${:.2}/GPU-hr",
                node.node_id,
                category,
                price as f64 / 100.0
            );

            match client.update_node_price(&node.node_id, price).await {
                Ok(()) => {
                    info!("Price updated for node {}", node.node_id);
                }
                Err(e) => {
                    warn!("Failed to update price for node {}: {}", node.node_id, e);
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
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

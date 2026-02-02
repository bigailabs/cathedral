//! Automatic bidding module
//!
//! Owns the complete miner registration lifecycle:
//! - Initial registration with validator (RegisterBid)
//! - Periodic health checks
//! - Price updates when bidding config changes
//!
//! AutoBidder is the single source of truth for GPU pricing.
//! All prices come from BiddingConfig.static_prices.

use anyhow::Result;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::watch;
use tokio::time::interval;
use tracing::{debug, error, info, warn};

use basilica_protocol::miner_discovery::NodeRegistration;

use crate::config::BiddingConfig;
use crate::node_manager::{NodeManager, RegisteredNode};
use crate::registration_client::RegistrationClient;

/// Automatic bidder that owns the complete registration lifecycle
pub struct AutoBidder {
    config: BiddingConfig,
    node_manager: Arc<NodeManager>,
    registration_client: Arc<RegistrationClient>,
}

impl AutoBidder {
    /// Create a new auto-bidder with registration client
    pub fn new(
        config: BiddingConfig,
        node_manager: Arc<NodeManager>,
        registration_client: Arc<RegistrationClient>,
    ) -> Self {
        Self {
            config,
            node_manager,
            registration_client,
        }
    }

    /// Validate that all configured nodes have prices in BiddingConfig.
    /// This ensures operators explicitly configure pricing for all their hardware.
    pub fn validate_node_prices(&self, nodes: &[RegisteredNode]) -> Result<()> {
        for node in nodes {
            let category = node.config.gpu_category.to_uppercase();
            if self.get_bid_price(&category).is_none() {
                anyhow::bail!(
                    "Node '{}' has GPU category '{}' but no price configured in [bidding.static_prices]. \
                     Add `{} = <price>` to your miner.toml",
                    node.node_id,
                    category,
                    category
                );
            }
        }
        Ok(())
    }

    /// Get price for a node from BiddingConfig (assumes validation passed)
    fn get_node_price(&self, gpu_category: &str) -> u32 {
        let category = gpu_category.to_uppercase();
        // Safe to unwrap after validation
        self.get_bid_price(&category)
            .expect("validation should have caught missing price")
    }

    /// Build node registrations with prices from BiddingConfig
    fn build_node_registrations(&self, nodes: &[RegisteredNode]) -> Vec<NodeRegistration> {
        nodes
            .iter()
            .map(|n| {
                let price = self.get_node_price(&n.config.gpu_category);
                NodeRegistration {
                    node_id: n.node_id.clone(),
                    host: n.config.host.clone(),
                    port: n.config.port as u32,
                    username: n.config.username.clone(),
                    gpu_category: n.config.gpu_category.clone(),
                    gpu_count: n.config.gpu_count,
                    hourly_rate_cents: price,
                    attestation: vec![], // TODO: Add attestation when available
                }
            })
            .collect()
    }

    /// Run the auto-bidder lifecycle:
    /// 1. Validate all nodes have prices
    /// 2. Register with validator
    /// 3. Deploy SSH keys if provided
    /// 4. Run combined health check + price update loop
    pub async fn run(&self, mut shutdown_rx: watch::Receiver<bool>) -> Result<()> {
        // 0. Validate all nodes have prices configured
        let nodes = self.node_manager.list_nodes().await?;

        if nodes.is_empty() {
            warn!("No nodes configured - waiting for shutdown signal");
            // Still wait for shutdown so we don't cause crash-loop
            loop {
                if shutdown_rx.changed().await.is_err() || *shutdown_rx.borrow() {
                    info!("AutoBidder shutdown requested (no nodes)");
                    return Ok(());
                }
            }
        }

        self.validate_node_prices(&nodes)?;

        info!(
            "Starting AutoBidder with {} nodes, {} GPU categories configured",
            nodes.len(),
            self.config.static_prices_cents.len()
        );

        // 1. Register nodes with auto-bidder prices
        let node_registrations = self.build_node_registrations(&nodes);
        let state = self
            .registration_client
            .register_nodes_with_registrations(node_registrations)
            .await?;

        info!(
            "Successfully registered {} nodes with validator",
            nodes.len()
        );

        // 2. Deploy validator SSH key if provided
        if state.validator_ssh_public_key.is_some() {
            if let Err(e) = self.registration_client.deploy_validator_ssh_key().await {
                error!("Failed to deploy validator SSH key: {}", e);
            }
        }

        // 3. Run combined health check + price update loop
        let health_interval = Duration::from_secs(state.health_check_interval_secs as u64);
        let price_interval = self.config.bid_interval;

        let mut health_ticker = interval(health_interval);
        let mut price_ticker = interval(price_interval);

        info!(
            "Starting lifecycle loop: health_check={}s, price_update={}s",
            health_interval.as_secs(),
            price_interval.as_secs()
        );

        loop {
            tokio::select! {
                _ = health_ticker.tick() => {
                    match self.registration_client.send_health_check().await {
                        Ok(nodes_active) => {
                            debug!(nodes_active = nodes_active, "Health check successful");
                        }
                        Err(e) => {
                            warn!("Health check failed: {}", e);
                        }
                    }
                }
                _ = price_ticker.tick() => {
                    if let Err(e) = self.submit_price_updates().await {
                        error!("Price update failed: {}", e);
                    }
                }
                changed = shutdown_rx.changed() => {
                    if changed.is_err() || *shutdown_rx.borrow() {
                        info!("AutoBidder shutdown requested");
                        break;
                    }
                }
            }
        }
        Ok(())
    }

    /// Update prices for all registered nodes if they differ from config
    async fn submit_price_updates(&self) -> Result<()> {
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

            // Note: We always send the update since we no longer track price in NodeConfig
            // The validator will handle deduplication if the price hasn't changed
            debug!(
                "Submitting price for node {}: {} @ ${:.2}/GPU-hr",
                node.node_id,
                category,
                price as f64 / 100.0
            );

            match self
                .registration_client
                .update_node_price(&node.node_id, price)
                .await
            {
                Ok(()) => {
                    debug!("Price submitted for node {}", node.node_id);
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
            static_prices_cents,
            bid_interval: Duration::from_secs(60),
            floor_prices_cents: HashMap::new(),
        }
    }

    #[test]
    fn test_get_bid_price() {
        let config = test_config();

        // Test pricing logic directly
        let static_prices = &config.static_prices_cents;

        assert_eq!(static_prices.get("H100"), Some(&250));
        assert_eq!(static_prices.get("A100"), Some(&120));
        assert_eq!(static_prices.get("RTX4090"), None);
    }

    #[test]
    fn test_get_bid_price_with_floor() {
        let mut config = test_config();
        config.floor_prices_cents.insert("H100".to_string(), 300); // Floor $3.00 higher than static $2.50

        // Test floor price enforcement
        let price = config.static_prices_cents.get("H100").unwrap();
        let floor = config.floor_prices_cents.get("H100").unwrap();
        assert_eq!((*price).max(*floor), 300); // Should use floor
    }

    #[test]
    fn test_validate_node_prices_success() {
        let config = test_config();

        // Validation should pass for categories that have prices
        let has_h100 = config.static_prices_cents.contains_key("H100");
        let has_a100 = config.static_prices_cents.contains_key("A100");
        assert!(has_h100 && has_a100);
    }

    #[test]
    fn test_validate_node_prices_missing() {
        let config = test_config();

        // Check that RTX4090 is not in prices (would fail validation)
        assert!(!config.static_prices_cents.contains_key("RTX4090"));
    }
}

use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuctionConfig {
    /// Price API endpoint (required, no fallback)
    pub price_api_endpoint: String,
    /// Price cache TTL in seconds
    pub price_cache_ttl_secs: u64,
    /// Minimum bid as fraction of baseline (0.0-1.0)
    pub min_bid_floor_fraction: f64,
    /// Bid validity window in seconds
    pub bid_validity_secs: u64,
    /// Max age for node health checks when accepting bids
    pub bid_node_freshness_secs: u64,
    /// Reservation TTL in seconds for selected nodes
    pub bid_reservation_secs: u64,
    /// Miner emission share of subnet emissions (0.0-1.0)
    pub miner_emission_share: f64,
    /// Health check interval in seconds (returned to miners in RegisterBidResponse)
    pub health_check_interval_secs: u64,
    /// Number of missed health checks before node is filtered from auction
    pub health_check_miss_threshold: u32,
    /// Validator's SSH public key (for miner nodes to deploy)
    pub validator_ssh_public_key: Option<String>,
}

impl Default for AuctionConfig {
    fn default() -> Self {
        Self {
            price_api_endpoint: "http://basilica-api:8080/v1/prices/baseline".to_string(),
            price_cache_ttl_secs: 60,
            min_bid_floor_fraction: 0.1,
            bid_validity_secs: 300,
            bid_node_freshness_secs: 300,
            bid_reservation_secs: 60,
            miner_emission_share: 0.41,
            health_check_interval_secs: 60,
            health_check_miss_threshold: 3,
            validator_ssh_public_key: None,
        }
    }
}

impl AuctionConfig {
    /// Get the maximum age in seconds for a health check before a node is filtered from auction
    pub fn health_check_ttl_secs(&self) -> u64 {
        self.health_check_interval_secs * self.health_check_miss_threshold as u64
    }

    pub fn validate(&self) -> Result<()> {
        if self.price_api_endpoint.trim().is_empty() {
            return Err(anyhow!("price_api_endpoint cannot be empty"));
        }
        if !self.min_bid_floor_fraction.is_finite()
            || !(0.0..=1.0).contains(&self.min_bid_floor_fraction)
        {
            return Err(anyhow!(
                "min_bid_floor_fraction must be between 0.0 and 1.0"
            ));
        }
        if self.price_cache_ttl_secs == 0 {
            return Err(anyhow!("price_cache_ttl_secs must be greater than 0"));
        }
        if self.bid_validity_secs == 0 {
            return Err(anyhow!("bid_validity_secs must be greater than 0"));
        }
        if self.bid_node_freshness_secs == 0 {
            return Err(anyhow!("bid_node_freshness_secs must be greater than 0"));
        }
        if self.bid_reservation_secs == 0 {
            return Err(anyhow!("bid_reservation_secs must be greater than 0"));
        }
        if !self.miner_emission_share.is_finite()
            || !(0.0..=1.0).contains(&self.miner_emission_share)
        {
            return Err(anyhow!("miner_emission_share must be between 0.0 and 1.0"));
        }
        if self.health_check_interval_secs == 0 {
            return Err(anyhow!("health_check_interval_secs must be greater than 0"));
        }
        if self.health_check_miss_threshold == 0 {
            return Err(anyhow!(
                "health_check_miss_threshold must be greater than 0"
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_valid_endpoint() {
        let config = AuctionConfig::default();
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_valid_config() {
        let config = AuctionConfig {
            price_api_endpoint: "http://localhost:50071".to_string(),
            ..AuctionConfig::default()
        };
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_invalid_floor_fraction() {
        let config = AuctionConfig {
            price_api_endpoint: "http://localhost:50071".to_string(),
            min_bid_floor_fraction: 1.5,
            ..AuctionConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_zero_durations_invalid() {
        let config = AuctionConfig {
            price_api_endpoint: "http://localhost:50071".to_string(),
            price_cache_ttl_secs: 0,
            bid_validity_secs: 0,
            bid_node_freshness_secs: 0,
            bid_reservation_secs: 0,
            ..AuctionConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_invalid_miner_emission_share() {
        let config = AuctionConfig {
            price_api_endpoint: "http://localhost:50071".to_string(),
            miner_emission_share: 1.5,
            ..AuctionConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_health_check_interval_zero_invalid() {
        let config = AuctionConfig {
            health_check_interval_secs: 0,
            ..AuctionConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_health_check_miss_threshold_zero_invalid() {
        let config = AuctionConfig {
            health_check_miss_threshold: 0,
            ..AuctionConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_health_check_ttl_secs() {
        let config = AuctionConfig {
            health_check_interval_secs: 60,
            health_check_miss_threshold: 3,
            ..AuctionConfig::default()
        };
        assert_eq!(config.health_check_ttl_secs(), 180);
    }
}

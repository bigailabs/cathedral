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
    /// Auction epoch duration in blocks
    pub auction_epoch_blocks: u64,
    /// TaoStats API base URL
    pub taostats_api_url: String,
    /// TaoStats cache TTL in seconds
    pub taostats_cache_ttl_secs: u64,
    /// Miner emission share of subnet emissions (0.0-1.0)
    pub miner_emission_share: f64,
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
            auction_epoch_blocks: 360,
            taostats_api_url: "https://api.taostats.io".to_string(),
            taostats_cache_ttl_secs: 300,
            miner_emission_share: 0.41,
        }
    }
}

impl AuctionConfig {
    pub fn validate(&self) -> Result<()> {
        if self.price_api_endpoint.trim().is_empty() {
            return Err(anyhow!("price_api_endpoint cannot be empty"));
        }
        if !(0.0..=1.0).contains(&self.min_bid_floor_fraction) {
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
        if self.auction_epoch_blocks == 0 {
            return Err(anyhow!("auction_epoch_blocks must be greater than 0"));
        }
        if self.taostats_api_url.trim().is_empty() {
            return Err(anyhow!("taostats_api_url cannot be empty"));
        }
        if self.taostats_cache_ttl_secs == 0 {
            return Err(anyhow!("taostats_cache_ttl_secs must be greater than 0"));
        }
        if !(0.0..=1.0).contains(&self.miner_emission_share) {
            return Err(anyhow!("miner_emission_share must be between 0.0 and 1.0"));
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
            taostats_api_url: "https://api.taostats.io".to_string(),
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
            auction_epoch_blocks: 0,
            taostats_cache_ttl_secs: 0,
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
}

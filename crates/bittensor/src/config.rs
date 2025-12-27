//! # Bittensor Configuration
//!
//! Configuration types for the Bittensor SDK, including network settings,
//! wallet configuration, and connection pool settings.

use serde::{Deserialize, Serialize};
use std::time::Duration;

/// Bittensor network configuration
///
/// # Example
///
/// ```
/// use bittensor::config::BittensorConfig;
///
/// let config = BittensorConfig::default();
/// assert_eq!(config.network, "finney");
/// ```
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BittensorConfig {
    /// Wallet name for operations
    pub wallet_name: String,

    /// Hotkey name for the neuron
    pub hotkey_name: String,

    /// Network to connect to ("finney", "test", or "local")
    pub network: String,

    /// Subnet netuid
    pub netuid: u16,

    /// Optional chain endpoint override
    pub chain_endpoint: Option<String>,

    /// Optional fallback chain endpoints for failover
    #[serde(default)]
    pub fallback_endpoints: Vec<String>,

    /// Weight setting interval in seconds
    pub weight_interval_secs: u64,

    /// Read-only mode (default: false)
    /// When true, wallet is only used for querying metagraph, not signing transactions
    #[serde(default)]
    pub read_only: bool,

    /// Connection pool size (default: 3)
    #[serde(default)]
    pub connection_pool_size: Option<usize>,

    /// Health check interval (default: 60 seconds)
    #[serde(default, with = "optional_duration_serde")]
    pub health_check_interval: Option<Duration>,

    /// Circuit breaker failure threshold (default: 5)
    #[serde(default)]
    pub circuit_breaker_threshold: Option<u32>,

    /// Circuit breaker recovery timeout (default: 60 seconds)
    #[serde(default, with = "optional_duration_serde")]
    pub circuit_breaker_recovery: Option<Duration>,
}

impl Default for BittensorConfig {
    fn default() -> Self {
        Self {
            wallet_name: "default".to_string(),
            hotkey_name: "default".to_string(),
            network: "finney".to_string(),
            netuid: 1,
            chain_endpoint: None,
            fallback_endpoints: Vec::new(),
            weight_interval_secs: 300, // 5 minutes
            read_only: false,
            connection_pool_size: Some(3),
            health_check_interval: Some(Duration::from_secs(60)),
            circuit_breaker_threshold: Some(5),
            circuit_breaker_recovery: Some(Duration::from_secs(60)),
        }
    }
}

impl BittensorConfig {
    /// Create a new configuration for the finney network
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::config::BittensorConfig;
    ///
    /// let config = BittensorConfig::finney("my_wallet", "my_hotkey", 1);
    /// assert_eq!(config.network, "finney");
    /// ```
    pub fn finney(wallet_name: &str, hotkey_name: &str, netuid: u16) -> Self {
        Self {
            wallet_name: wallet_name.to_string(),
            hotkey_name: hotkey_name.to_string(),
            network: "finney".to_string(),
            netuid,
            ..Default::default()
        }
    }

    /// Create a new configuration for the test network
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::config::BittensorConfig;
    ///
    /// let config = BittensorConfig::testnet("my_wallet", "my_hotkey", 1);
    /// assert_eq!(config.network, "test");
    /// ```
    pub fn testnet(wallet_name: &str, hotkey_name: &str, netuid: u16) -> Self {
        Self {
            wallet_name: wallet_name.to_string(),
            hotkey_name: hotkey_name.to_string(),
            network: "test".to_string(),
            netuid,
            ..Default::default()
        }
    }

    /// Create a new configuration for a local network
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::config::BittensorConfig;
    ///
    /// let config = BittensorConfig::local("my_wallet", "my_hotkey", 1);
    /// assert_eq!(config.network, "local");
    /// ```
    pub fn local(wallet_name: &str, hotkey_name: &str, netuid: u16) -> Self {
        Self {
            wallet_name: wallet_name.to_string(),
            hotkey_name: hotkey_name.to_string(),
            network: "local".to_string(),
            netuid,
            ..Default::default()
        }
    }

    /// Get the chain endpoint, auto-detecting based on network if not explicitly configured
    ///
    /// # Panics
    ///
    /// Panics if the network is not one of: finney, test, local
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::config::BittensorConfig;
    ///
    /// let config = BittensorConfig::default();
    /// let endpoint = config.get_chain_endpoint();
    /// assert!(endpoint.starts_with("wss://"));
    /// ```
    pub fn get_chain_endpoint(&self) -> String {
        self.chain_endpoint
            .clone()
            .unwrap_or_else(|| match self.network.as_str() {
                "local" => "ws://127.0.0.1:9944".to_string(),
                "finney" => "wss://entrypoint-finney.opentensor.ai:443".to_string(),
                "test" => "wss://test.finney.opentensor.ai:443".to_string(),
                _ => panic!(
                    "Unknown network: {}. Valid networks are: finney, test, local",
                    self.network
                ),
            })
    }

    /// Get all chain endpoints including fallbacks
    ///
    /// Returns the primary endpoint followed by any configured fallback endpoints.
    /// If no fallbacks are configured, network-specific defaults are added.
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::config::BittensorConfig;
    ///
    /// let config = BittensorConfig::default();
    /// let endpoints = config.get_chain_endpoints();
    /// assert!(!endpoints.is_empty());
    /// ```
    pub fn get_chain_endpoints(&self) -> Vec<String> {
        let mut endpoints = vec![self.get_chain_endpoint()];

        // Add configured fallback endpoints
        endpoints.extend(self.fallback_endpoints.clone());

        // Add network-specific default fallbacks if not already configured
        if self.fallback_endpoints.is_empty() {
            match self.network.as_str() {
                "finney" => {
                    endpoints.push("wss://entrypoint-finney.opentensor.ai:443".to_string());
                }
                "test" => {
                    endpoints.push("wss://test.finney.opentensor.ai:443".to_string());
                }
                _ => {}
            }
        }

        // Deduplicate endpoints while preserving order
        let mut seen = std::collections::HashSet::new();
        endpoints.retain(|endpoint| seen.insert(endpoint.clone()));

        endpoints
    }

    /// Validate the configuration
    ///
    /// # Returns
    ///
    /// * `Ok(())` if the configuration is valid
    /// * `Err(String)` with a description of the validation error
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::config::BittensorConfig;
    ///
    /// let config = BittensorConfig::default();
    /// assert!(config.validate().is_ok());
    /// ```
    pub fn validate(&self) -> Result<(), String> {
        if self.wallet_name.is_empty() {
            return Err("Wallet name cannot be empty".to_string());
        }

        if self.hotkey_name.is_empty() {
            return Err("Hotkey name cannot be empty".to_string());
        }

        if self.netuid == 0 {
            return Err("Netuid must be greater than 0".to_string());
        }

        if self.weight_interval_secs == 0 {
            return Err("Weight interval must be greater than 0 seconds".to_string());
        }

        match self.network.as_str() {
            "finney" | "test" | "local" => Ok(()),
            _ => Err(format!(
                "Unknown network: {}. Valid networks are: finney, test, local",
                self.network
            )),
        }
    }

    /// Set a custom chain endpoint
    pub fn with_endpoint(mut self, endpoint: &str) -> Self {
        self.chain_endpoint = Some(endpoint.to_string());
        self
    }

    /// Set fallback endpoints
    pub fn with_fallback_endpoints(mut self, endpoints: Vec<String>) -> Self {
        self.fallback_endpoints = endpoints;
        self
    }

    /// Set connection pool size
    pub fn with_pool_size(mut self, size: usize) -> Self {
        self.connection_pool_size = Some(size);
        self
    }

    /// Set read-only mode
    pub fn with_read_only(mut self, read_only: bool) -> Self {
        self.read_only = read_only;
        self
    }
}

/// Serde helper for optional Duration fields
mod optional_duration_serde {
    use serde::{Deserialize, Deserializer, Serialize, Serializer};
    use std::time::Duration;

    pub fn serialize<S>(value: &Option<Duration>, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        match value {
            Some(duration) => duration.as_secs().serialize(serializer),
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<Option<Duration>, D::Error>
    where
        D: Deserializer<'de>,
    {
        let opt: Option<u64> = Option::deserialize(deserializer)?;
        Ok(opt.map(Duration::from_secs))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let config = BittensorConfig::default();
        assert_eq!(config.wallet_name, "default");
        assert_eq!(config.hotkey_name, "default");
        assert_eq!(config.network, "finney");
        assert_eq!(config.netuid, 1);
        assert!(!config.read_only);
    }

    #[test]
    fn test_finney_config() {
        let config = BittensorConfig::finney("test_wallet", "test_hotkey", 42);
        assert_eq!(config.wallet_name, "test_wallet");
        assert_eq!(config.hotkey_name, "test_hotkey");
        assert_eq!(config.network, "finney");
        assert_eq!(config.netuid, 42);
    }

    #[test]
    fn test_testnet_config() {
        let config = BittensorConfig::testnet("wallet", "hotkey", 1);
        assert_eq!(config.network, "test");
    }

    #[test]
    fn test_local_config() {
        let config = BittensorConfig::local("wallet", "hotkey", 1);
        assert_eq!(config.network, "local");
        assert_eq!(config.get_chain_endpoint(), "ws://127.0.0.1:9944");
    }

    #[test]
    fn test_endpoint_resolution() {
        let finney = BittensorConfig::finney("w", "h", 1);
        assert_eq!(
            finney.get_chain_endpoint(),
            "wss://entrypoint-finney.opentensor.ai:443"
        );

        let test = BittensorConfig::testnet("w", "h", 1);
        assert_eq!(
            test.get_chain_endpoint(),
            "wss://test.finney.opentensor.ai:443"
        );

        let local = BittensorConfig::local("w", "h", 1);
        assert_eq!(local.get_chain_endpoint(), "ws://127.0.0.1:9944");
    }

    #[test]
    fn test_custom_endpoint() {
        let config = BittensorConfig::default().with_endpoint("wss://custom.endpoint:443");
        assert_eq!(config.get_chain_endpoint(), "wss://custom.endpoint:443");
    }

    #[test]
    fn test_fallback_endpoints() {
        let config = BittensorConfig::default();
        let endpoints = config.get_chain_endpoints();
        assert!(!endpoints.is_empty());
        // Primary endpoint should be first
        assert_eq!(endpoints[0], config.get_chain_endpoint());
    }

    #[test]
    fn test_validation_success() {
        let config = BittensorConfig::default();
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_validation_empty_wallet() {
        let config = BittensorConfig {
            wallet_name: String::new(),
            ..Default::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_validation_empty_hotkey() {
        let config = BittensorConfig {
            hotkey_name: String::new(),
            ..Default::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_validation_zero_netuid() {
        let config = BittensorConfig {
            netuid: 0,
            ..Default::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_validation_invalid_network() {
        let config = BittensorConfig {
            network: "invalid".to_string(),
            ..Default::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    #[should_panic(expected = "Unknown network")]
    fn test_invalid_network_endpoint() {
        let config = BittensorConfig {
            network: "invalid".to_string(),
            ..Default::default()
        };
        config.get_chain_endpoint();
    }

    #[test]
    fn test_builder_pattern() {
        let config = BittensorConfig::finney("w", "h", 1)
            .with_endpoint("wss://custom:443")
            .with_pool_size(5)
            .with_read_only(true);

        assert_eq!(config.get_chain_endpoint(), "wss://custom:443");
        assert_eq!(config.connection_pool_size, Some(5));
        assert!(config.read_only);
    }

    #[test]
    fn test_serialization() {
        let config = BittensorConfig::default();
        let json = serde_json::to_string(&config).unwrap();
        let deserialized: BittensorConfig = serde_json::from_str(&json).unwrap();
        assert_eq!(config.wallet_name, deserialized.wallet_name);
        assert_eq!(config.network, deserialized.network);
    }
}

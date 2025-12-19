use anyhow::Result;
use basilica_common::error::ConfigurationError;
use figment::{
    providers::{Env, Format, Serialized, Toml},
    Figment,
};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::time::Duration;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PaymentsConfig {
    pub service: ServiceConfig,
    pub database: DatabaseConfig,
    pub grpc: GrpcConfig,
    pub http: HttpConfig,
    pub blockchain: BlockchainConfig,
    pub treasury: TreasuryConfig,
    pub price_oracle: PriceOracleConfig,
    pub billing: BillingConfig,
    pub reconciliation: ReconciliationConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceConfig {
    pub name: String,
    pub environment: String,
    pub log_level: String,
    pub metrics_enabled: bool,
    pub service_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DatabaseConfig {
    pub url: String,
    pub max_connections: u32,
    pub min_connections: u32,
    pub connect_timeout_seconds: u64,
    pub acquire_timeout_seconds: u64,
    pub idle_timeout_seconds: u64,
    pub max_lifetime_seconds: u64,
    pub enable_ssl: bool,
    pub ssl_ca_cert_path: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GrpcConfig {
    pub listen_address: String,
    pub port: u16,
    pub max_message_size: usize,
    pub keepalive_interval_seconds: Option<u64>,
    pub keepalive_timeout_seconds: Option<u64>,
    pub tls_enabled: bool,
    pub tls_cert_path: Option<PathBuf>,
    pub tls_key_path: Option<PathBuf>,
    pub max_concurrent_requests: Option<usize>,
    pub max_concurrent_streams: Option<u32>,
    pub request_timeout_seconds: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HttpConfig {
    pub listen_address: String,
    pub port: u16,
    pub cors_enabled: bool,
    pub cors_allowed_origins: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BlockchainConfig {
    pub websocket_url: String,
    /// Optional fallback websocket endpoints for failover
    #[serde(default)]
    pub fallback_websocket_urls: Vec<String>,
    pub ss58_prefix: u16,
    pub connection_timeout_seconds: u64,
    pub retry_interval_seconds: u64,
    /// Maximum consecutive failures before skipping to current block (default: 5)
    #[serde(default = "default_max_block_retries")]
    pub max_block_retries: u32,
    /// Number of blocks kept by non-archive nodes (default: 256, ~51 minutes)
    #[serde(default = "default_block_retention")]
    pub block_retention_threshold: u32,
    /// Maximum block gap before forcing skip to current (default: 300, ~1 hour)
    #[serde(default = "default_max_block_gap")]
    pub max_block_gap: u32,
    /// Enable automatic reconnection on connection failures (default: true)
    #[serde(default = "default_auto_reconnect")]
    pub auto_reconnect: bool,
}

fn default_max_block_retries() -> u32 {
    5
}

fn default_block_retention() -> u32 {
    256
}

fn default_max_block_gap() -> u32 {
    300
}

fn default_auto_reconnect() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TreasuryConfig {
    pub aead_key_hex: String,
    pub tao_decimals: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PriceOracleConfig {
    pub update_interval_seconds: u64,
    pub max_price_age_seconds: u64,
    pub request_timeout_seconds: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BillingConfig {
    pub grpc_endpoint: String,
    pub connection_timeout_seconds: u64,
    pub request_timeout_seconds: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReconciliationConfig {
    pub enabled: bool,
    pub sweep_interval_seconds: u64,
    pub coldwallet_address_ss58: String,
    #[serde(deserialize_with = "deserialize_string_or_number")]
    pub minimum_threshold_plancks: String,
    #[serde(deserialize_with = "deserialize_string_or_number")]
    pub target_balance_plancks: String,
    #[serde(deserialize_with = "deserialize_string_or_number")]
    pub estimated_fee_plancks: String,
    pub dry_run_mode: bool,
    pub max_retries: u32,
    /// Maximum sweeps per cycle to prevent transaction flooding (default: 50)
    #[serde(default = "default_max_sweeps_per_cycle")]
    pub max_sweeps_per_cycle: u32,
}

fn default_max_sweeps_per_cycle() -> u32 {
    50
}

fn deserialize_string_or_number<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::{self, Visitor};
    use std::fmt;

    struct StringOrNumber;

    impl<'de> Visitor<'de> for StringOrNumber {
        type Value = String;

        fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
            formatter.write_str("a string or number")
        }

        fn visit_str<E>(self, value: &str) -> Result<String, E>
        where
            E: de::Error,
        {
            Ok(value.to_string())
        }

        fn visit_string<E>(self, value: String) -> Result<String, E>
        where
            E: de::Error,
        {
            Ok(value)
        }

        fn visit_u64<E>(self, value: u64) -> Result<String, E>
        where
            E: de::Error,
        {
            Ok(value.to_string())
        }

        fn visit_i64<E>(self, value: i64) -> Result<String, E>
        where
            E: de::Error,
        {
            Ok(value.to_string())
        }

        fn visit_u128<E>(self, value: u128) -> Result<String, E>
        where
            E: de::Error,
        {
            Ok(value.to_string())
        }

        fn visit_i128<E>(self, value: i128) -> Result<String, E>
        where
            E: de::Error,
        {
            Ok(value.to_string())
        }
    }

    deserializer.deserialize_any(StringOrNumber)
}

impl Default for PaymentsConfig {
    fn default() -> Self {
        Self {
            service: ServiceConfig {
                name: "basilica-payments".to_string(),
                environment: "development".to_string(),
                log_level: "info".to_string(),
                metrics_enabled: true,
                service_id: Uuid::new_v4().to_string(),
            },
            database: DatabaseConfig {
                url: "postgres://payments@localhost:5432/basilica_payments".to_string(),
                max_connections: 32,
                min_connections: 5,
                connect_timeout_seconds: 30,
                acquire_timeout_seconds: 30,
                idle_timeout_seconds: 600,
                max_lifetime_seconds: 1800,
                enable_ssl: false,
                ssl_ca_cert_path: None,
            },
            grpc: GrpcConfig {
                listen_address: "0.0.0.0".to_string(),
                port: 50061,
                max_message_size: 4 * 1024 * 1024, // 4MB
                keepalive_interval_seconds: Some(300),
                keepalive_timeout_seconds: Some(20),
                tls_enabled: false,
                tls_cert_path: None,
                tls_key_path: None,
                max_concurrent_requests: Some(1000),
                max_concurrent_streams: Some(100),
                request_timeout_seconds: Some(60),
            },
            http: HttpConfig {
                listen_address: "0.0.0.0".to_string(),
                port: 8082,
                cors_enabled: true,
                cors_allowed_origins: vec!["*".to_string()],
            },
            blockchain: BlockchainConfig {
                websocket_url: "ws://localhost:9944".to_string(),
                fallback_websocket_urls: Vec::new(),
                ss58_prefix: 42,
                connection_timeout_seconds: 30,
                retry_interval_seconds: 5,
                max_block_retries: 5,
                block_retention_threshold: 256,
                max_block_gap: 300,
                auto_reconnect: true,
            },
            treasury: TreasuryConfig {
                aead_key_hex: "0000000000000000000000000000000000000000000000000000000000000000"
                    .to_string(),
                tao_decimals: 9,
            },
            price_oracle: PriceOracleConfig {
                update_interval_seconds: 60,
                max_price_age_seconds: 300,
                request_timeout_seconds: 10,
            },
            billing: BillingConfig {
                grpc_endpoint: "http://localhost:50051".to_string(),
                connection_timeout_seconds: 30,
                request_timeout_seconds: 60,
            },
            reconciliation: ReconciliationConfig {
                enabled: false,
                sweep_interval_seconds: 300,
                coldwallet_address_ss58: String::new(),
                minimum_threshold_plancks: "10000000".to_string(),
                target_balance_plancks: "5000000".to_string(),
                estimated_fee_plancks: "1000000".to_string(),
                dry_run_mode: true,
                max_retries: 3,
                max_sweeps_per_cycle: 50,
            },
        }
    }
}

impl PaymentsConfig {
    pub fn load(path_override: Option<PathBuf>) -> Result<PaymentsConfig, ConfigurationError> {
        let default_config = PaymentsConfig::default();

        let mut figment = Figment::from(Serialized::defaults(default_config));

        if let Some(path) = path_override {
            if path.exists() {
                figment = figment.merge(Toml::file(&path));
            }
        } else {
            let default_path = PathBuf::from("payments.toml");
            if default_path.exists() {
                figment = figment.merge(Toml::file(default_path));
            }
        }

        figment = figment.merge(Env::prefixed("PAYMENTS_").split("__"));

        figment
            .extract()
            .map_err(|e| ConfigurationError::ParseError {
                details: e.to_string(),
            })
    }

    pub fn load_from_file(path: &Path) -> Result<PaymentsConfig, ConfigurationError> {
        Self::load(Some(path.to_path_buf()))
    }

    pub fn apply_env_overrides(
        config: &mut PaymentsConfig,
        prefix: &str,
    ) -> Result<(), ConfigurationError> {
        let figment = Figment::from(Serialized::defaults(config.clone()))
            .merge(Env::prefixed(prefix).split("__"));

        *config = figment
            .extract()
            .map_err(|e| ConfigurationError::ParseError {
                details: e.to_string(),
            })?;

        Ok(())
    }

    pub fn validate(&self) -> Result<(), ConfigurationError> {
        if self.database.url.is_empty() {
            return Err(ConfigurationError::InvalidValue {
                key: "database.url".to_string(),
                value: String::new(),
                reason: "Database URL cannot be empty".to_string(),
            });
        }

        if self.database.max_connections < self.database.min_connections {
            return Err(ConfigurationError::ValidationFailed {
                details: format!(
                    "database.max_connections ({}) must be >= min_connections ({})",
                    self.database.max_connections, self.database.min_connections
                ),
            });
        }

        if self.grpc.port == 0 {
            return Err(ConfigurationError::ValidationFailed {
                details: "grpc.port must be non-zero".to_string(),
            });
        }

        if self.grpc.tls_enabled
            && (self.grpc.tls_cert_path.is_none() || self.grpc.tls_key_path.is_none())
        {
            return Err(ConfigurationError::ValidationFailed {
                details: "TLS cert and key paths required when TLS is enabled".to_string(),
            });
        }

        if self.treasury.aead_key_hex.is_empty() {
            return Err(ConfigurationError::ValidationFailed {
                details: "treasury.aead_key_hex must not be empty".to_string(),
            });
        }

        // SECURITY: Reject default all-zeros AEAD key in production or when reconciliation is enabled
        let is_default_key = self.treasury.aead_key_hex
            == "0000000000000000000000000000000000000000000000000000000000000000";
        if is_default_key {
            if self.service.environment == "production" {
                return Err(ConfigurationError::ValidationFailed {
                    details:
                        "SECURITY: Default AEAD key (all zeros) is not allowed in production. \
                        Set PAYMENTS_TREASURY__AEAD_KEY_HEX to a secure 32-byte hex key."
                            .to_string(),
                });
            }
            if self.reconciliation.enabled {
                return Err(ConfigurationError::ValidationFailed {
                    details: "SECURITY: Default AEAD key (all zeros) is not allowed when reconciliation is enabled. \
                        Set PAYMENTS_TREASURY__AEAD_KEY_HEX to a secure 32-byte hex key."
                        .to_string(),
                });
            }
        }

        // Validate AEAD key format (must be 64 hex chars = 32 bytes)
        if self.treasury.aead_key_hex.len() != 64 {
            return Err(ConfigurationError::ValidationFailed {
                details: format!(
                    "treasury.aead_key_hex must be exactly 64 hex characters (32 bytes), got {}",
                    self.treasury.aead_key_hex.len()
                ),
            });
        }
        if !self
            .treasury
            .aead_key_hex
            .chars()
            .all(|c| c.is_ascii_hexdigit())
        {
            return Err(ConfigurationError::ValidationFailed {
                details: "treasury.aead_key_hex must contain only hex characters (0-9, a-f, A-F)"
                    .to_string(),
            });
        }

        if self.blockchain.websocket_url.is_empty() {
            return Err(ConfigurationError::ValidationFailed {
                details: "blockchain.websocket_url must not be empty".to_string(),
            });
        }

        if self.blockchain.max_block_retries == 0 {
            return Err(ConfigurationError::ValidationFailed {
                details: "blockchain.max_block_retries must be > 0".to_string(),
            });
        }

        if self.blockchain.block_retention_threshold == 0 {
            return Err(ConfigurationError::ValidationFailed {
                details: "blockchain.block_retention_threshold must be > 0".to_string(),
            });
        }

        if self.blockchain.max_block_gap < self.blockchain.block_retention_threshold {
            return Err(ConfigurationError::ValidationFailed {
                details: format!(
                    "blockchain.max_block_gap ({}) should be >= block_retention_threshold ({})",
                    self.blockchain.max_block_gap, self.blockchain.block_retention_threshold
                ),
            });
        }

        if self.reconciliation.enabled && self.reconciliation.coldwallet_address_ss58.is_empty() {
            return Err(ConfigurationError::ValidationFailed {
                details: "reconciliation.coldwallet_address_ss58 required when enabled".to_string(),
            });
        }

        if self.reconciliation.enabled {
            let min_threshold: u128 = self
                .reconciliation
                .minimum_threshold_plancks
                .parse()
                .map_err(|_| ConfigurationError::ValidationFailed {
                    details: "Invalid reconciliation.minimum_threshold_plancks".to_string(),
                })?;
            let target: u128 =
                self.reconciliation
                    .target_balance_plancks
                    .parse()
                    .map_err(|_| ConfigurationError::ValidationFailed {
                        details: "Invalid reconciliation.target_balance_plancks".to_string(),
                    })?;
            let estimated_fee: u128 =
                self.reconciliation
                    .estimated_fee_plancks
                    .parse()
                    .map_err(|_| ConfigurationError::ValidationFailed {
                        details: "Invalid reconciliation.estimated_fee_plancks".to_string(),
                    })?;

            if target + estimated_fee >= min_threshold {
                return Err(ConfigurationError::ValidationFailed {
                    details:
                        "reconciliation.target_balance + estimated_fee must be < minimum_threshold"
                            .to_string(),
                });
            }
        }

        Ok(())
    }

    pub fn warnings(&self) -> Vec<String> {
        let mut warnings = Vec::new();

        if !self.database.enable_ssl && self.service.environment == "production" {
            warnings.push("Database SSL is disabled in production environment".to_string());
        }

        if !self.grpc.tls_enabled && self.service.environment == "production" {
            warnings.push("gRPC TLS is disabled in production environment".to_string());
        }

        if self.treasury.aead_key_hex
            == "0000000000000000000000000000000000000000000000000000000000000000"
        {
            warnings
                .push("Using default AEAD key - generate a secure key for production".to_string());
        }

        warnings
    }

    pub fn connect_timeout(&self) -> Duration {
        Duration::from_secs(self.database.connect_timeout_seconds)
    }

    pub fn acquire_timeout(&self) -> Duration {
        Duration::from_secs(self.database.acquire_timeout_seconds)
    }

    pub fn idle_timeout(&self) -> Duration {
        Duration::from_secs(self.database.idle_timeout_seconds)
    }

    pub fn max_lifetime(&self) -> Duration {
        Duration::from_secs(self.database.max_lifetime_seconds)
    }

    pub fn blockchain_connection_timeout(&self) -> Duration {
        Duration::from_secs(self.blockchain.connection_timeout_seconds)
    }

    pub fn blockchain_retry_interval(&self) -> Duration {
        Duration::from_secs(self.blockchain.retry_interval_seconds)
    }

    pub fn price_oracle_update_interval(&self) -> Duration {
        Duration::from_secs(self.price_oracle.update_interval_seconds)
    }

    pub fn price_oracle_max_age(&self) -> Duration {
        Duration::from_secs(self.price_oracle.max_price_age_seconds)
    }

    pub fn price_oracle_request_timeout(&self) -> Duration {
        Duration::from_secs(self.price_oracle.request_timeout_seconds)
    }

    pub fn billing_connection_timeout(&self) -> Duration {
        Duration::from_secs(self.billing.connection_timeout_seconds)
    }

    pub fn billing_request_timeout(&self) -> Duration {
        Duration::from_secs(self.billing.request_timeout_seconds)
    }

    pub fn reconciliation_sweep_interval(&self) -> Duration {
        Duration::from_secs(self.reconciliation.sweep_interval_seconds)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_reconciliation_config_deserialize_numeric_string() {
        let json = r#"{
            "enabled": true,
            "sweep_interval_seconds": 300,
            "coldwallet_address_ss58": "5FUE3WJ438ymnLYmSpkGcagFrFWaMBuVr28VMgZRAqTJD62e",
            "minimum_threshold_plancks": 1000000000,
            "target_balance_plancks": "550000000",
            "estimated_fee_plancks": 50000000,
            "dry_run_mode": false,
            "max_retries": 3
        }"#;

        let config: ReconciliationConfig = serde_json::from_str(json).unwrap();

        assert_eq!(config.minimum_threshold_plancks, "1000000000");
        assert_eq!(config.target_balance_plancks, "550000000");
        assert_eq!(config.estimated_fee_plancks, "50000000");
        assert!(config.enabled);
        assert!(!config.dry_run_mode);
    }

    #[test]
    fn test_reconciliation_config_deserialize_string_only() {
        let json = r#"{
            "enabled": false,
            "sweep_interval_seconds": 300,
            "coldwallet_address_ss58": "5FUE3WJ438ymnLYmSpkGcagFrFWaMBuVr28VMgZRAqTJD62e",
            "minimum_threshold_plancks": "1000000000",
            "target_balance_plancks": "550000000",
            "estimated_fee_plancks": "50000000",
            "dry_run_mode": true,
            "max_retries": 3
        }"#;

        let config: ReconciliationConfig = serde_json::from_str(json).unwrap();

        assert_eq!(config.minimum_threshold_plancks, "1000000000");
        assert_eq!(config.target_balance_plancks, "550000000");
        assert_eq!(config.estimated_fee_plancks, "50000000");
    }

    #[test]
    fn test_default_aead_key_rejected_in_production() {
        let mut config = PaymentsConfig::default();
        config.service.environment = "production".to_string();
        // Default config has all-zeros AEAD key

        let result = config.validate();
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.to_string().contains("Default AEAD key"));
        assert!(err.to_string().contains("production"));
    }

    #[test]
    fn test_default_aead_key_rejected_when_reconciliation_enabled() {
        let mut config = PaymentsConfig::default();
        config.service.environment = "development".to_string();
        config.reconciliation.enabled = true;
        config.reconciliation.coldwallet_address_ss58 =
            "5FUE3WJ438ymnLYmSpkGcagFrFWaMBuVr28VMgZRAqTJD62e".to_string();

        let result = config.validate();
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.to_string().contains("Default AEAD key"));
        assert!(err.to_string().contains("reconciliation"));
    }

    #[test]
    fn test_valid_aead_key_accepted() {
        let mut config = PaymentsConfig::default();
        config.service.environment = "production".to_string();
        config.treasury.aead_key_hex =
            "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789".to_string();

        let result = config.validate();
        assert!(result.is_ok());
    }

    #[test]
    fn test_invalid_aead_key_length_rejected() {
        let mut config = PaymentsConfig::default();
        config.treasury.aead_key_hex = "abcdef".to_string(); // Too short

        let result = config.validate();
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.to_string().contains("64 hex characters"));
    }

    #[test]
    fn test_invalid_aead_key_chars_rejected() {
        let mut config = PaymentsConfig::default();
        config.treasury.aead_key_hex =
            "ghijkl0123456789abcdef0123456789abcdef0123456789abcdef0123456789".to_string();

        let result = config.validate();
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.to_string().contains("hex characters"));
    }

    #[test]
    fn test_default_aead_key_allowed_in_development_without_reconciliation() {
        let mut config = PaymentsConfig::default();
        config.service.environment = "development".to_string();
        config.reconciliation.enabled = false;
        // Default config has all-zeros AEAD key but development environment

        let result = config.validate();
        assert!(result.is_ok());
    }
}

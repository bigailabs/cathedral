use crate::error::{AggregatorError, Result};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Config {
    pub server: ServerConfig,
    pub cache: CacheConfig,
    pub providers: ProvidersConfig,
    pub database: DatabaseConfig,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ServerConfig {
    #[serde(default = "default_host")]
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
}

fn default_host() -> String {
    "0.0.0.0".to_string()
}

fn default_port() -> u16 {
    8080
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CacheConfig {
    #[serde(default = "default_ttl")]
    pub ttl_seconds: u64,
}

fn default_ttl() -> u64 {
    45
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ProvidersConfig {
    pub datacrunch: ProviderConfig,
    #[serde(default)]
    pub hyperstack: ProviderConfig,
    #[serde(default)]
    pub lambda: ProviderConfig,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ProviderConfig {
    #[serde(default)]
    pub enabled: bool,
    pub client_id: Option<String>,
    pub client_secret: Option<String>,
    #[serde(default = "default_cooldown")]
    pub cooldown_seconds: u64,
    #[serde(default = "default_timeout")]
    pub timeout_seconds: u64,
    pub api_base_url: Option<String>,
}

impl Default for ProviderConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            client_id: None,
            client_secret: None,
            cooldown_seconds: default_cooldown(),
            timeout_seconds: default_timeout(),
            api_base_url: None,
        }
    }
}

fn default_cooldown() -> u64 {
    30
}

fn default_timeout() -> u64 {
    10
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DatabaseConfig {
    #[serde(default = "default_db_path")]
    pub path: String,
}

fn default_db_path() -> String {
    "aggregator.db".to_string()
}

impl Config {
    /// Load configuration from file and environment variables
    pub fn load(config_path: Option<PathBuf>) -> Result<Self> {
        let mut builder = config::Config::builder();

        // Load from file if provided
        if let Some(path) = config_path {
            builder = builder.add_source(config::File::from(path));
        }

        // Add environment variable overrides
        builder = builder.add_source(
            config::Environment::with_prefix("AGGREGATOR")
                .separator("__")
                .try_parsing(true),
        );

        let config = builder
            .build()
            .map_err(|e| AggregatorError::Config(e.to_string()))?;

        config
            .try_deserialize()
            .map_err(|e| AggregatorError::Config(e.to_string()))
    }

    /// Validate configuration
    pub fn validate(&self) -> Result<()> {
        // Check at least one provider is enabled
        let any_enabled = self.providers.datacrunch.enabled
            || self.providers.hyperstack.enabled
            || self.providers.lambda.enabled;

        if !any_enabled {
            return Err(AggregatorError::Config(
                "At least one provider must be enabled".to_string(),
            ));
        }

        // Check enabled providers have credentials
        if self.providers.datacrunch.enabled {
            if self.providers.datacrunch.client_id.is_none() {
                return Err(AggregatorError::Config(
                    "DataCrunch provider enabled but client_id not set".to_string(),
                ));
            }
            if self.providers.datacrunch.client_secret.is_none() {
                return Err(AggregatorError::Config(
                    "DataCrunch provider enabled but client_secret not set".to_string(),
                ));
            }
        }

        if self.providers.hyperstack.enabled {
            if self.providers.hyperstack.client_id.is_none() {
                return Err(AggregatorError::Config(
                    "Hyperstack provider enabled but client_id not set".to_string(),
                ));
            }
            if self.providers.hyperstack.client_secret.is_none() {
                return Err(AggregatorError::Config(
                    "Hyperstack provider enabled but client_secret not set".to_string(),
                ));
            }
        }

        if self.providers.lambda.enabled {
            if self.providers.lambda.client_id.is_none() {
                return Err(AggregatorError::Config(
                    "Lambda provider enabled but client_id not set".to_string(),
                ));
            }
            if self.providers.lambda.client_secret.is_none() {
                return Err(AggregatorError::Config(
                    "Lambda provider enabled but client_secret not set".to_string(),
                ));
            }
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_config_validation_no_providers() {
        let config = Config {
            server: ServerConfig {
                host: "0.0.0.0".to_string(),
                port: 8080,
            },
            cache: CacheConfig { ttl_seconds: 45 },
            providers: ProvidersConfig {
                datacrunch: ProviderConfig::default(),
                hyperstack: ProviderConfig::default(),
                lambda: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: "test.db".to_string(),
            },
        };

        assert!(config.validate().is_err());
    }

    #[test]
    fn test_config_validation_missing_credentials() {
        let config = Config {
            server: ServerConfig {
                host: "0.0.0.0".to_string(),
                port: 8080,
            },
            cache: CacheConfig { ttl_seconds: 45 },
            providers: ProvidersConfig {
                datacrunch: ProviderConfig {
                    enabled: true,
                    client_id: None,
                    client_secret: None,
                    ..Default::default()
                },
                hyperstack: ProviderConfig::default(),
                lambda: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: "test.db".to_string(),
            },
        };

        assert!(config.validate().is_err());
    }

    #[test]
    fn test_config_validation_valid() {
        let config = Config {
            server: ServerConfig {
                host: "0.0.0.0".to_string(),
                port: 8080,
            },
            cache: CacheConfig { ttl_seconds: 45 },
            providers: ProvidersConfig {
                datacrunch: ProviderConfig {
                    enabled: true,
                    client_id: Some("test-client-id".to_string()),
                    client_secret: Some("test-client-secret".to_string()),
                    api_base_url: Some("https://api.datacrunch.io/v1".to_string()),
                    ..Default::default()
                },
                hyperstack: ProviderConfig::default(),
                lambda: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: "test.db".to_string(),
            },
        };

        assert!(config.validate().is_ok());
    }
}

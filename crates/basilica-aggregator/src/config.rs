use crate::error::{AggregatorError, Result};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Config {
    pub server: ServerConfig,
    pub cache: CacheConfig,
    pub providers: ProvidersConfig,
    pub database: DatabaseConfig,
    #[serde(default)]
    pub vip: Option<VipConfig>,
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
    #[serde(default)]
    pub datacrunch: ProviderConfig,
    #[serde(default)]
    pub hyperstack: Option<HyperstackConfig>,
    #[serde(default)]
    pub lambda: ProviderConfig,
    #[serde(default)]
    pub hydrahost: ProviderConfig,
}

/// Hyperstack-specific configuration with required webhook fields
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct HyperstackConfig {
    /// Hyperstack API key
    pub api_key: String,
    /// Webhook secret token for authenticating callbacks
    pub webhook_secret: String,
    /// Base URL for webhooks, e.g., "https://api.basilica.ai"
    pub callback_base_url: String,
}

impl HyperstackConfig {
    /// Build callback URL with token as query parameter
    pub fn callback_url(&self) -> String {
        format!(
            "{}/webhooks/cloud-provider/hyperstack?token={}",
            self.callback_base_url.trim_end_matches('/'),
            self.webhook_secret
        )
    }
}

/// Authentication configuration enum for different provider types
/// The type is automatically determined based on which fields are present in ProviderConfig
#[derive(Debug, Clone)]
pub enum AuthConfig {
    OAuth {
        client_id: String,
        client_secret: String,
    },
    ApiKey {
        api_key: String,
    },
}

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
pub struct ProviderConfig {
    /// OAuth client ID (for OAuth providers like DataCrunch)
    pub client_id: Option<String>,
    /// OAuth client secret (for OAuth providers like DataCrunch)
    pub client_secret: Option<String>,
    /// API key (for API key providers like Lambda)
    pub api_key: Option<String>,
}

impl ProviderConfig {
    /// Check if this provider is enabled (has auth configured)
    pub fn is_enabled(&self) -> bool {
        self.get_auth().is_some()
    }

    /// Get authentication config if provider is configured
    pub fn get_auth(&self) -> Option<AuthConfig> {
        match (&self.client_id, &self.client_secret, &self.api_key) {
            (Some(client_id), Some(client_secret), None) => Some(AuthConfig::OAuth {
                client_id: client_id.clone(),
                client_secret: client_secret.clone(),
            }),
            (None, None, Some(api_key)) => Some(AuthConfig::ApiKey {
                api_key: api_key.clone(),
            }),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DatabaseConfig {
    #[serde(default = "default_db_path")]
    pub path: String,
}

fn default_db_path() -> String {
    "aggregator.db".to_string()
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct VipConfig {
    /// Local CSV file path (for dev/testing)
    #[serde(default)]
    pub csv_file_path: Option<String>,
    /// S3 bucket name (for production)
    #[serde(default)]
    pub s3_bucket: Option<String>,
    /// S3 object key (for production)
    #[serde(default)]
    pub s3_key: Option<String>,
    /// S3 region (for production, e.g., "us-east-2")
    #[serde(default)]
    pub s3_region: Option<String>,
    /// Polling interval in seconds (default: 60)
    #[serde(default = "default_vip_poll_interval")]
    pub poll_interval_secs: u64,
    /// Mock mode: If set, use mock data with this user ID instead of CSV
    #[serde(default)]
    pub mock_user_id: Option<String>,
}

impl Default for VipConfig {
    fn default() -> Self {
        Self {
            csv_file_path: None,
            s3_bucket: None,
            s3_key: None,
            s3_region: None,
            poll_interval_secs: default_vip_poll_interval(),
            mock_user_id: None,
        }
    }
}

fn default_vip_poll_interval() -> u64 {
    60
}

impl VipConfig {
    /// Check if VIP is properly configured (mock, local CSV, or S3)
    pub fn is_configured(&self) -> bool {
        self.is_mock_mode() || self.has_csv_source()
    }

    /// Check if a CSV source is configured (local file or S3)
    pub fn has_csv_source(&self) -> bool {
        self.has_s3_source() || self.has_local_csv()
    }

    /// Check if S3 source is configured
    pub fn has_s3_source(&self) -> bool {
        self.s3_bucket
            .as_ref()
            .map(|s| !s.is_empty())
            .unwrap_or(false)
            && self.s3_key.as_ref().map(|s| !s.is_empty()).unwrap_or(false)
    }

    /// Check if local CSV file is configured
    pub fn has_local_csv(&self) -> bool {
        self.csv_file_path
            .as_ref()
            .map(|s| !s.is_empty())
            .unwrap_or(false)
    }

    /// Check if mock mode is enabled
    pub fn is_mock_mode(&self) -> bool {
        self.mock_user_id
            .as_ref()
            .map(|s| !s.is_empty())
            .unwrap_or(false)
    }
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
        // Check at least one provider is enabled (has auth configured)
        let any_enabled = self.providers.datacrunch.is_enabled()
            || self.providers.hyperstack.is_some()
            || self.providers.lambda.is_enabled()
            || self.providers.hydrahost.is_enabled();

        if !any_enabled {
            return Err(AggregatorError::Config(
                "At least one provider must be configured (has auth)".to_string(),
            ));
        }

        if let Some(hyperstack) = &self.providers.hyperstack {
            if hyperstack.api_key.trim().is_empty() {
                return Err(AggregatorError::Config(
                    "Hyperstack api_key must be non-empty".to_string(),
                ));
            }
            if hyperstack.webhook_secret.trim().is_empty() {
                return Err(AggregatorError::Config(
                    "Hyperstack webhook_secret must be non-empty".to_string(),
                ));
            }
            if !is_url_safe_token(&hyperstack.webhook_secret) {
                return Err(AggregatorError::Config(
                    "Hyperstack webhook_secret must be URL-safe (A-Z a-z 0-9 - _ . ~)".to_string(),
                ));
            }
            if hyperstack
                .webhook_secret
                .chars()
                .any(|c| c.is_ascii_uppercase())
            {
                tracing::warn!(
                    "Hyperstack webhook_secret contains uppercase characters; \
                     Hyperstack lowercases callback URLs so token comparison will be case-insensitive"
                );
            }
            if hyperstack.callback_base_url.trim().is_empty() {
                return Err(AggregatorError::Config(
                    "Hyperstack callback_base_url must be non-empty".to_string(),
                ));
            }
        }

        Ok(())
    }

    /// Create a default config for testing (no providers enabled)
    pub fn default_for_tests() -> Self {
        Self {
            server: ServerConfig {
                host: default_host(),
                port: default_port(),
            },
            cache: CacheConfig {
                ttl_seconds: default_ttl(),
            },
            providers: ProvidersConfig {
                datacrunch: ProviderConfig::default(),
                hyperstack: None,
                lambda: ProviderConfig::default(),
                hydrahost: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: default_db_path(),
            },
            vip: None,
        }
    }
}

fn is_url_safe_token(value: &str) -> bool {
    value
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '~'))
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
                hyperstack: None,
                lambda: ProviderConfig::default(),
                hydrahost: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: "test.db".to_string(),
            },
            vip: None,
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
                datacrunch: ProviderConfig::default(),
                hyperstack: None,
                lambda: ProviderConfig::default(),
                hydrahost: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: "test.db".to_string(),
            },
            vip: None,
        };

        assert!(config.validate().is_err());
    }

    #[test]
    fn test_config_validation_valid_oauth() {
        let config = Config {
            server: ServerConfig {
                host: "0.0.0.0".to_string(),
                port: 8080,
            },
            cache: CacheConfig { ttl_seconds: 45 },
            providers: ProvidersConfig {
                datacrunch: ProviderConfig {
                    client_id: Some("test-client-id".to_string()),
                    client_secret: Some("test-client-secret".to_string()),
                    api_key: None,
                },
                hyperstack: None,
                lambda: ProviderConfig::default(),
                hydrahost: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: "test.db".to_string(),
            },
            vip: None,
        };

        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_config_validation_valid_api_key() {
        let config = Config {
            server: ServerConfig {
                host: "0.0.0.0".to_string(),
                port: 8080,
            },
            cache: CacheConfig { ttl_seconds: 45 },
            providers: ProvidersConfig {
                datacrunch: ProviderConfig::default(),
                hyperstack: None,
                lambda: ProviderConfig {
                    client_id: None,
                    client_secret: None,
                    api_key: Some("test-api-key".to_string()),
                },
                hydrahost: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: "test.db".to_string(),
            },
            vip: None,
        };

        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_config_validation_valid_hyperstack() {
        let config = Config {
            server: ServerConfig {
                host: "0.0.0.0".to_string(),
                port: 8080,
            },
            cache: CacheConfig { ttl_seconds: 45 },
            providers: ProvidersConfig {
                datacrunch: ProviderConfig::default(),
                hyperstack: Some(HyperstackConfig {
                    api_key: "test-api-key".to_string(),
                    webhook_secret: "test-webhook-secret".to_string(),
                    callback_base_url: "https://api.example.com".to_string(),
                }),
                lambda: ProviderConfig::default(),
                hydrahost: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: "test.db".to_string(),
            },
            vip: None,
        };

        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_hyperstack_config_callback_url() {
        let config = HyperstackConfig {
            api_key: "test-key".to_string(),
            webhook_secret: "secret123".to_string(),
            callback_base_url: "https://api.example.com/".to_string(),
        };

        assert_eq!(
            config.callback_url(),
            "https://api.example.com/webhooks/cloud-provider/hyperstack?token=secret123"
        );
    }

    #[test]
    fn test_config_validation_invalid_hyperstack_webhook_secret() {
        let config = Config {
            server: ServerConfig {
                host: "0.0.0.0".to_string(),
                port: 8080,
            },
            cache: CacheConfig { ttl_seconds: 45 },
            providers: ProvidersConfig {
                datacrunch: ProviderConfig::default(),
                hyperstack: Some(HyperstackConfig {
                    api_key: "test-api-key".to_string(),
                    webhook_secret: "bad&token".to_string(),
                    callback_base_url: "https://api.example.com".to_string(),
                }),
                lambda: ProviderConfig::default(),
                hydrahost: ProviderConfig::default(),
            },
            database: DatabaseConfig {
                path: "test.db".to_string(),
            },
            vip: None,
        };

        assert!(config.validate().is_err());
    }

    #[test]
    fn test_config_deserialization_oauth() {
        let toml = r#"
[server]
host = "0.0.0.0"
port = 8080

[cache]
ttl_seconds = 45

[database]
path = "test.db"

[providers.datacrunch]
api_base_url = "https://api.datacrunch.io/v1"
client_id = "test-id"
client_secret = "test-secret"
        "#;

        let config: Config = toml::from_str(toml).unwrap();
        assert!(config.providers.datacrunch.is_enabled());
        match config.providers.datacrunch.get_auth() {
            Some(AuthConfig::OAuth {
                client_id,
                client_secret,
            }) => {
                assert_eq!(client_id, "test-id");
                assert_eq!(client_secret, "test-secret");
            }
            _ => panic!("Expected OAuth auth"),
        }
    }

    #[test]
    fn test_config_deserialization_api_key() {
        let toml = r#"
[server]
host = "0.0.0.0"
port = 8080

[cache]
ttl_seconds = 45

[database]
path = "test.db"

[providers.lambda]
api_base_url = "https://cloud.lambdalabs.com/api/v1"
api_key = "test-key"
        "#;

        let config: Config = toml::from_str(toml).unwrap();
        assert!(config.providers.lambda.is_enabled());
        match config.providers.lambda.get_auth() {
            Some(AuthConfig::ApiKey { api_key }) => {
                assert_eq!(api_key, "test-key");
            }
            _ => panic!("Expected ApiKey auth"),
        }
    }
}

use async_trait::async_trait;
use std::fmt;

pub mod cloudflare;

pub type Result<T> = std::result::Result<T, DnsError>;

#[derive(Debug, thiserror::Error)]
pub enum DnsError {
    #[error("Failed to create DNS record: {0}")]
    CreateFailed(String),

    #[error("Failed to delete DNS record: {0}")]
    DeleteFailed(String),

    #[error("DNS record not found: {0}")]
    RecordNotFound(String),

    #[error("Invalid configuration: {0}")]
    ConfigError(String),

    #[error("API error: {0}")]
    ApiError(String),
}

#[async_trait]
pub trait DnsProvider: Send + Sync + fmt::Debug {
    async fn create_record(&self, subdomain: &str, target: &str) -> Result<String>;
    async fn delete_record(&self, subdomain: &str) -> Result<()>;
}

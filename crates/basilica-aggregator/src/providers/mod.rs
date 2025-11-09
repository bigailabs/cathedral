use crate::error::Result;
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use async_trait::async_trait;

pub mod datacrunch;
pub mod http_utils;
pub mod hydrahost;
pub mod hyperstack;
pub mod lambda;

#[async_trait]
pub trait Provider: Send + Sync {
    /// Unique provider identifier
    fn provider_id(&self) -> ProviderEnum;

    /// Fetch GPU offerings from provider API
    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>>;

    /// Health check for provider API
    async fn health_check(&self) -> Result<ProviderHealth>;

    /// Register SSH key with provider
    /// Returns the provider's SSH key ID
    async fn create_ssh_key(&self, name: String, public_key: String) -> Result<String>;

    /// Delete SSH key from provider
    async fn delete_ssh_key(&self, provider_key_id: &str) -> Result<()>;

    /// List SSH keys (optional, for debugging/admin)
    async fn list_ssh_keys(&self) -> Result<Vec<String>> {
        Ok(vec![])
    }
}

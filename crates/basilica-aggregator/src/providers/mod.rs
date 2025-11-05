use crate::error::Result;
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use async_trait::async_trait;

pub mod datacrunch;

#[async_trait]
pub trait Provider: Send + Sync {
    /// Unique provider identifier
    fn provider_id(&self) -> ProviderEnum;

    /// Fetch GPU offerings from provider API
    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>>;

    /// Health check for provider API
    async fn health_check(&self) -> Result<ProviderHealth>;
}

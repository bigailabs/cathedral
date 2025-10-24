use crate::error::{BillingError, Result};
use crate::pricing::types::{DynamicPricingConfig, GpuPrice, PriceQueryFilter, PriceSource};
use async_trait::async_trait;
use tracing::info;

/// Trait for price providers that fetch GPU prices from external sources
#[async_trait]
pub trait PriceProvider: Send + Sync {
    /// Get the name of this provider (e.g., "marketplace")
    fn name(&self) -> &str;

    /// Fetch GPU prices from this provider
    async fn fetch_prices(&self, filter: &PriceQueryFilter) -> Result<Vec<GpuPrice>>;

    /// Test if the provider is available and healthy
    async fn health_check(&self) -> bool;
}

// Provider implementations
pub mod marketplace;

pub use marketplace::MarketplaceProvider;

/// Create providers from configuration
pub fn create_providers(config: &DynamicPricingConfig) -> Result<Vec<Box<dyn PriceProvider>>> {
    if !config.enabled {
        info!("Dynamic pricing is disabled");
        return Ok(Vec::new());
    }

    let mut providers: Vec<Box<dyn PriceProvider>> = Vec::new();

    for source in &config.sources {
        match source {
            PriceSource::Marketplace => {
                info!("Enabling Marketplace price provider");

                let api_key = config.marketplace_api_key.clone().ok_or_else(|| {
                    BillingError::ConfigurationError {
                        message: "Marketplace API key is required".to_string(),
                    }
                })?;

                let provider = MarketplaceProvider::new(
                    config.marketplace_api_url.clone(),
                    api_key,
                    config.marketplace_available_only,
                )?;

                providers.push(Box::new(provider));
            }
            PriceSource::Custom { url } => {
                info!("Custom price provider not yet implemented: {}", url);
                // TODO: Implement custom provider with configurable URL
            }
        }
    }

    if providers.is_empty() {
        return Err(BillingError::ConfigurationError {
            message: "No price sources configured".to_string(),
        });
    }

    info!(
        "Created {} price providers from {} configured sources",
        providers.len(),
        config.sources.len()
    );
    Ok(providers)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_create_providers_disabled() {
        let config = DynamicPricingConfig {
            enabled: false,
            ..Default::default()
        };

        let providers = create_providers(&config).unwrap();
        assert_eq!(providers.len(), 0);
    }

    #[test]
    fn test_create_providers_marketplace() {
        let config = DynamicPricingConfig {
            enabled: true,
            sources: vec![PriceSource::Marketplace],
            marketplace_api_key: Some("test-key".to_string()),
            ..Default::default()
        };

        let providers = create_providers(&config).unwrap();
        assert_eq!(providers.len(), 1);
        assert_eq!(providers[0].name(), "marketplace");
    }

    #[test]
    fn test_create_providers_no_api_key() {
        let config = DynamicPricingConfig {
            enabled: true,
            sources: vec![PriceSource::Marketplace],
            marketplace_api_key: None,
            ..Default::default()
        };

        let result = create_providers(&config);
        assert!(result.is_err());
    }

    #[test]
    fn test_create_providers_empty_sources_error() {
        let config = DynamicPricingConfig {
            enabled: true,
            sources: Vec::new(),
            ..Default::default()
        };

        let result = create_providers(&config);
        assert!(result.is_err());
    }
}

use crate::error::{BillingError, Result};
use crate::pricing::providers::PriceProvider;
use crate::pricing::types::{GpuPrice, PriceQueryFilter};
use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use rust_decimal::prelude::FromPrimitive;
use rust_decimal::Decimal;
use serde::Deserialize;
use std::time::Duration;
use tracing::{debug, warn};

/// Marketplace provider - fetches GPU prices from marketplace aggregator API
/// This aggregates prices from multiple providers (VastAI, RunPod, Lambda Labs, etc.)
pub struct MarketplaceProvider {
    client: Client,
    api_url: String,
    api_key: String,
    available_only: bool,
}

/// Marketplace API response structure
#[derive(Debug, Deserialize)]
struct MarketplaceResponse {
    instance_types: Vec<MarketplaceInstanceType>,
}

#[derive(Debug, Deserialize)]
struct MarketplaceInstanceType {
    cloud: String,
    shade_instance_type: String,
    hourly_price: u64, // in cents
    configuration: MarketplaceConfiguration,
    #[serde(default)]
    availability: Vec<MarketplaceAvailability>,
}

#[derive(Debug, Deserialize)]
struct MarketplaceConfiguration {
    gpu_type: String,
    #[serde(default)]
    #[allow(dead_code)]
    num_gpus: Option<u32>,
    #[serde(default)]
    vram_per_gpu_in_gb: Option<u32>,
    #[serde(default)]
    #[allow(dead_code)]
    interconnect: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MarketplaceAvailability {
    region: String,
    available: bool,
    #[serde(default)]
    #[allow(dead_code)]
    display_name: Option<String>,
}

impl MarketplaceProvider {
    /// Create a new marketplace provider
    pub fn new(api_url: String, api_key: String, available_only: bool) -> Result<Self> {
        if api_key.is_empty() {
            return Err(BillingError::ConfigurationError {
                message: "Marketplace API key is required".to_string(),
            });
        }

        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .unwrap_or_else(|_| Client::new());

        Ok(Self {
            client,
            api_url,
            api_key,
            available_only,
        })
    }

    /// Convert marketplace instance to GpuPrice entries (one per available region)
    fn convert_instance(&self, instance: MarketplaceInstanceType) -> Vec<GpuPrice> {
        let mut prices = Vec::new();

        // Convert price from cents to dollars
        let market_price = match Decimal::from_f64((instance.hourly_price as f64) / 100.0) {
            Some(p) if p > Decimal::ZERO => p,
            _ => {
                warn!(
                    "Invalid price for {}: {} cents",
                    instance.shade_instance_type, instance.hourly_price
                );
                return prices;
            }
        };

        // Create one price entry per available region (if filtering by availability)
        let regions_to_include: Vec<&MarketplaceAvailability> = if self.available_only {
            instance
                .availability
                .iter()
                .filter(|av| av.available)
                .collect()
        } else {
            instance.availability.iter().collect()
        };

        // If no regions or empty availability, create one entry with no location
        if regions_to_include.is_empty() {
            prices.push(GpuPrice {
                gpu_model: instance.configuration.gpu_type.clone(),
                vram_gb: instance.configuration.vram_per_gpu_in_gb,
                market_price_per_hour: market_price,
                discounted_price_per_hour: market_price,
                discount_percent: Decimal::ZERO,
                source: "marketplace".to_string(),
                provider: instance.cloud.clone(),
                location: None,
                instance_name: Some(instance.shade_instance_type.clone()),
                updated_at: Utc::now(),
                is_spot: false, // TODO: Check if marketplace API provides spot instance info
            });
        } else {
            for av in regions_to_include {
                prices.push(GpuPrice {
                    gpu_model: instance.configuration.gpu_type.clone(),
                    vram_gb: instance.configuration.vram_per_gpu_in_gb,
                    market_price_per_hour: market_price,
                    discounted_price_per_hour: market_price,
                    discount_percent: Decimal::ZERO,
                    source: "marketplace".to_string(),
                    provider: instance.cloud.clone(),
                    location: Some(av.region.clone()),
                    instance_name: Some(format!("{}-{}", instance.shade_instance_type, av.region)),
                    updated_at: Utc::now(),
                    is_spot: false,
                });
            }
        }

        prices
    }

    /// Fetch prices for a specific GPU type
    async fn fetch_gpu_type(&self, gpu_type: &str) -> Result<Vec<GpuPrice>> {
        let mut params = vec![("gpu_type", gpu_type), ("sort", "price")];

        if self.available_only {
            params.push(("available", "true"));
        }

        let url = format!(
            "{}/instances/types?{}",
            self.api_url,
            params
                .iter()
                .map(|(k, v)| format!("{}={}", k, urlencoding::encode(v)))
                .collect::<Vec<_>>()
                .join("&")
        );

        debug!("Fetching marketplace prices for {}: {}", gpu_type, url);

        let response = self
            .client
            .get(&url)
            .header("X-API-KEY", &self.api_key)
            .send()
            .await
            .map_err(|e| BillingError::ExternalApiError {
                provider: "marketplace".to_string(),
                details: format!("Failed to fetch from marketplace: {}", e),
            })?;

        if !response.status().is_success() {
            return Err(BillingError::ExternalApiError {
                provider: "marketplace".to_string(),
                details: format!(
                    "Marketplace API returned status {} for GPU {}",
                    response.status(),
                    gpu_type
                ),
            });
        }

        let marketplace_response: MarketplaceResponse =
            response
                .json()
                .await
                .map_err(|e| BillingError::ExternalApiError {
                    provider: "marketplace".to_string(),
                    details: format!("Failed to parse marketplace response: {}", e),
                })?;

        debug!(
            "Received {} instance types from marketplace for {}",
            marketplace_response.instance_types.len(),
            gpu_type
        );

        // Convert instances to prices
        let mut prices = Vec::new();
        for instance in marketplace_response.instance_types {
            prices.extend(self.convert_instance(instance));
        }

        Ok(prices)
    }
}

impl Default for MarketplaceProvider {
    fn default() -> Self {
        Self {
            client: Client::new(),
            api_url: "https://api.shadeform.ai/v1".to_string(),
            api_key: String::new(),
            available_only: true,
        }
    }
}

#[async_trait]
impl PriceProvider for MarketplaceProvider {
    fn name(&self) -> &str {
        "marketplace"
    }

    async fn fetch_prices(&self, filter: &PriceQueryFilter) -> Result<Vec<GpuPrice>> {
        debug!("Fetching prices from marketplace API: {}", self.api_url);

        // Determine which GPU models to query
        let gpu_models = match &filter.gpu_models {
            Some(models) => models.clone(),
            None => {
                // Default GPU models to query
                vec![
                    "H100".to_string(),
                    "H200".to_string(),
                    "A100".to_string(),
                    "A40".to_string(),
                    "RTX4090".to_string(),
                    "RTX3090".to_string(),
                    "V100".to_string(),
                    "L40".to_string(),
                ]
            }
        };

        let mut all_prices = Vec::new();

        // Fetch prices for each GPU model
        for gpu_model in gpu_models {
            match self.fetch_gpu_type(&gpu_model).await {
                Ok(prices) => all_prices.extend(prices),
                Err(e) => {
                    warn!("Failed to fetch prices for {}: {}", gpu_model, e);
                    // Continue with other GPU types even if one fails
                }
            }
        }

        // Apply additional filters
        let mut prices = all_prices;

        if let Some(min_vram) = filter.min_vram_gb {
            prices.retain(|p| p.vram_gb.map(|vram| vram >= min_vram).unwrap_or(false));
        }

        if let Some(max_price) = filter.max_price {
            prices.retain(|p| p.market_price_per_hour <= max_price);
        }

        if let Some(ref providers) = filter.providers {
            prices.retain(|p| {
                providers
                    .iter()
                    .any(|prov| p.provider.eq_ignore_ascii_case(prov))
            });
        }

        if filter.spot_only {
            prices.retain(|p| p.is_spot);
        }

        debug!(
            "Returning {} prices from marketplace after filtering",
            prices.len()
        );

        Ok(prices)
    }

    async fn health_check(&self) -> bool {
        debug!("Performing health check for marketplace");

        // Simple health check: try to fetch H100 prices
        let url = format!("{}/instances/types?gpu_type=H100&sort=price", self.api_url);

        match self
            .client
            .get(&url)
            .header("X-API-KEY", &self.api_key)
            .send()
            .await
        {
            Ok(response) => {
                let is_healthy = response.status().is_success();
                debug!("Marketplace health check: {}", is_healthy);
                is_healthy
            }
            Err(e) => {
                warn!("Marketplace health check failed: {}", e);
                false
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_marketplace_provider_creation() {
        let provider = MarketplaceProvider::new(
            "https://api.test.com".to_string(),
            "test-key".to_string(),
            true,
        );
        assert!(provider.is_ok());
        assert_eq!(provider.unwrap().name(), "marketplace");
    }

    #[test]
    fn test_marketplace_provider_no_api_key() {
        let provider =
            MarketplaceProvider::new("https://api.test.com".to_string(), "".to_string(), true);
        assert!(provider.is_err());
    }

    #[test]
    fn test_convert_instance_no_regions() {
        let provider = MarketplaceProvider::new(
            "https://api.test.com".to_string(),
            "test-key".to_string(),
            false,
        )
        .unwrap();

        let instance = MarketplaceInstanceType {
            cloud: "testcloud".to_string(),
            shade_instance_type: "test-h100".to_string(),
            hourly_price: 299, // $2.99
            configuration: MarketplaceConfiguration {
                gpu_type: "H100".to_string(),
                num_gpus: Some(1),
                vram_per_gpu_in_gb: Some(80),
                interconnect: Some("pcie".to_string()),
            },
            availability: vec![],
        };

        let prices = provider.convert_instance(instance);
        assert_eq!(prices.len(), 1);
        assert_eq!(prices[0].gpu_model, "H100");
        assert_eq!(prices[0].vram_gb, Some(80));
        assert_eq!(
            prices[0].market_price_per_hour,
            Decimal::from_f64(2.99).unwrap()
        );
        assert_eq!(prices[0].provider, "testcloud");
        assert_eq!(prices[0].location, None);
    }

    #[test]
    fn test_convert_instance_with_regions() {
        let provider = MarketplaceProvider::new(
            "https://api.test.com".to_string(),
            "test-key".to_string(),
            false,
        )
        .unwrap();

        let instance = MarketplaceInstanceType {
            cloud: "testcloud".to_string(),
            shade_instance_type: "test-a100".to_string(),
            hourly_price: 189, // $1.89
            configuration: MarketplaceConfiguration {
                gpu_type: "A100".to_string(),
                num_gpus: Some(1),
                vram_per_gpu_in_gb: Some(80),
                interconnect: Some("nvlink".to_string()),
            },
            availability: vec![
                MarketplaceAvailability {
                    region: "us-east".to_string(),
                    available: true,
                    display_name: Some("US East".to_string()),
                },
                MarketplaceAvailability {
                    region: "eu-west".to_string(),
                    available: true,
                    display_name: Some("EU West".to_string()),
                },
            ],
        };

        let prices = provider.convert_instance(instance);
        assert_eq!(prices.len(), 2);
        assert_eq!(prices[0].location, Some("us-east".to_string()));
        assert_eq!(prices[1].location, Some("eu-west".to_string()));
        assert_eq!(prices[0].gpu_model, "A100");
        assert_eq!(prices[1].gpu_model, "A100");
    }

    #[test]
    fn test_convert_instance_available_only() {
        let provider = MarketplaceProvider::new(
            "https://api.test.com".to_string(),
            "test-key".to_string(),
            true, // available_only = true
        )
        .unwrap();

        let instance = MarketplaceInstanceType {
            cloud: "testcloud".to_string(),
            shade_instance_type: "test-h100".to_string(),
            hourly_price: 299,
            configuration: MarketplaceConfiguration {
                gpu_type: "H100".to_string(),
                num_gpus: Some(1),
                vram_per_gpu_in_gb: Some(80),
                interconnect: None,
            },
            availability: vec![
                MarketplaceAvailability {
                    region: "us-east".to_string(),
                    available: true,
                    display_name: None,
                },
                MarketplaceAvailability {
                    region: "eu-west".to_string(),
                    available: false, // Not available
                    display_name: None,
                },
            ],
        };

        let prices = provider.convert_instance(instance);
        // Should only get the available region
        assert_eq!(prices.len(), 1);
        assert_eq!(prices[0].location, Some("us-east".to_string()));
    }
}

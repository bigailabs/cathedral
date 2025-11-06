use super::normalize::{get_gpu_memory, normalize_gpu_type, normalize_region};
use super::types::ListingsResponse;
use crate::error::{AggregatorError, Result};
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use crate::providers::Provider;
use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use rust_decimal::Decimal;
use std::str::FromStr;
use std::time::Duration;

pub struct HydraHostProvider {
    client: Client,
    api_key: String,
    base_url: String,
}

impl HydraHostProvider {
    pub fn new(api_key: String, base_url: String, timeout_seconds: u64) -> Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(timeout_seconds))
            .build()
            .map_err(|e| AggregatorError::Provider {
                provider: "hydrahost".to_string(),
                message: format!("Failed to create HTTP client: {}", e),
            })?;

        Ok(Self {
            client,
            api_key,
            base_url,
        })
    }

    async fn fetch_listings(&self) -> Result<ListingsResponse> {
        let url = format!("{}/api/marketplace/listings", self.base_url);

        tracing::debug!("Fetching listings from HydraHost: {}", url);

        let response = self
            .client
            .get(&url)
            .header("X-API-KEY", &self.api_key) // HydraHost uses X-API-KEY header
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "hydrahost".to_string(),
                message: format!("Failed to fetch listings: {}", e),
            })?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            tracing::error!("HydraHost API returned error: {} - {}", status, error_text);
            return Err(AggregatorError::Provider {
                provider: "hydrahost".to_string(),
                message: format!("API returned status: {} - {}", status, error_text),
            });
        }

        let listings_response: ListingsResponse =
            response
                .json()
                .await
                .map_err(|e| AggregatorError::Provider {
                    provider: "hydrahost".to_string(),
                    message: format!("Failed to parse listings response: {}", e),
                })?;

        Ok(listings_response)
    }
}

#[async_trait]
impl Provider for HydraHostProvider {
    fn provider_id(&self) -> ProviderEnum {
        ProviderEnum::HydraHost
    }

    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>> {
        let listings_response = self.fetch_listings().await?;

        let fetched_at = Utc::now();
        let mut offerings = Vec::new();

        // Iterate through marketplace listings
        for listing in listings_response {
            // Skip listings with no GPUs
            if listing.specs.gpu.count == 0 {
                continue;
            }

            // Get GPU model - either from specs or infer from other fields
            let gpu_model = listing
                .specs
                .gpu
                .model
                .as_deref()
                .unwrap_or("unknown");

            // Normalize GPU type
            let gpu_type = normalize_gpu_type(gpu_model);

            // Get GPU memory
            let gpu_memory_gb = get_gpu_memory(gpu_model);

            // Normalize region to "global"
            let region = normalize_region(&listing.location);

            // Convert pricing to Decimal
            let hourly_rate = Decimal::from_str(&listing.price.hourly.total.to_string())
                .unwrap_or(Decimal::ZERO);

            // HydraHost doesn't explicitly provide spot pricing in this endpoint
            let spot_rate = None;

            // Check availability based on status
            // "on demand" means available, other statuses might indicate unavailable
            let availability = listing.status.to_lowercase() == "on demand";

            // Create offering with unique ID using listing ID
            let offering = GpuOffering {
                id: format!("hydrahost-{}", listing.id),
                provider: ProviderEnum::HydraHost,
                gpu_type,
                gpu_memory_gb,
                gpu_count: listing.specs.gpu.count,
                system_memory_gb: listing.specs.memory,
                vcpu_count: listing.specs.cpu.vcpus,
                region,
                hourly_rate,
                spot_rate,
                availability,
                fetched_at,
                raw_metadata: serde_json::to_value(&listing).unwrap_or_default(),
            };

            offerings.push(offering);
        }

        tracing::info!("Fetched {} offerings from HydraHost", offerings.len());
        Ok(offerings)
    }

    async fn health_check(&self) -> Result<ProviderHealth> {
        match self.fetch_listings().await {
            Ok(_) => Ok(ProviderHealth {
                provider: ProviderEnum::HydraHost,
                is_healthy: true,
                last_success_at: Some(Utc::now()),
                last_error: None,
            }),
            Err(e) => Ok(ProviderHealth {
                provider: ProviderEnum::HydraHost,
                is_healthy: false,
                last_success_at: None,
                last_error: Some(e.to_string()),
            }),
        }
    }
}

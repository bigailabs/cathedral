use super::normalize::{normalize_gpu_type, normalize_region, parse_gpu_memory};
use super::types::FlavorsResponse;
use crate::error::{AggregatorError, Result};
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use crate::providers::Provider;
use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use rust_decimal::Decimal;
use std::time::Duration;

pub struct HyperstackProvider {
    client: Client,
    api_key: String,
    base_url: String,
}

impl HyperstackProvider {
    pub fn new(api_key: String, base_url: String, timeout_seconds: u64) -> Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(timeout_seconds))
            .build()
            .map_err(|e| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to create HTTP client: {}", e),
            })?;

        Ok(Self {
            client,
            api_key,
            base_url,
        })
    }

    async fn fetch_flavors(&self) -> Result<FlavorsResponse> {
        let url = format!("{}/core/flavors", self.base_url);

        tracing::debug!("Fetching flavors from Hyperstack: {}", url);

        let response = self
            .client
            .get(&url)
            .header("api_key", &self.api_key) // Hyperstack uses custom header
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to fetch flavors: {}", e),
            })?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            tracing::error!("Hyperstack API returned error: {} - {}", status, error_text);
            return Err(AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("API returned status: {} - {}", status, error_text),
            });
        }

        let flavors_response: FlavorsResponse =
            response
                .json()
                .await
                .map_err(|e| AggregatorError::Provider {
                    provider: "hyperstack".to_string(),
                    message: format!("Failed to parse flavors response: {}", e),
                })?;

        Ok(flavors_response)
    }
}

#[async_trait]
impl Provider for HyperstackProvider {
    fn provider_id(&self) -> ProviderEnum {
        ProviderEnum::Hyperstack
    }

    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>> {
        let flavors_response = self.fetch_flavors().await?;

        let fetched_at = Utc::now();
        let mut offerings = Vec::new();

        // Iterate through GPU/region groups
        for group in flavors_response.data {
            // Skip CPU-only groups (empty gpu string)
            if group.gpu.is_empty() {
                continue;
            }

            // Normalize GPU type from group's GPU string
            let gpu_type = normalize_gpu_type(&group.gpu);

            // Parse GPU memory from group's GPU string (e.g., "A100-80G-PCIe" -> 80)
            let gpu_memory_gb = parse_gpu_memory(&group.gpu).unwrap_or_else(|| {
                tracing::warn!(
                    "Failed to parse GPU memory from group GPU: {}",
                    group.gpu
                );
                0
            });

            // Normalize region to "global" (consistent with DataCrunch)
            let region = normalize_region(&group.region_name);

            // Iterate through flavors in this group
            for flavor in group.flavors {
                // Skip flavors with no GPUs
                if flavor.gpu_count == 0 {
                    continue;
                }

                // Convert RAM from float GB to u32
                let system_memory_gb = flavor.ram.round() as u32;

                // Hyperstack API doesn't include pricing in flavors endpoint
                // Set to 0 - would need separate pricing API call
                let hourly_rate = Decimal::ZERO;
                let spot_rate = None;

                // Use stock_available from flavor
                let availability = flavor.stock_available;

                // Create offering with unique ID using flavor ID
                let offering = GpuOffering {
                    id: format!("hyperstack-{}", flavor.id),
                    provider: ProviderEnum::Hyperstack,
                    gpu_type: gpu_type.clone(),
                    gpu_memory_gb,
                    gpu_count: flavor.gpu_count,
                    system_memory_gb,
                    vcpu_count: flavor.cpu,
                    region: region.clone(),
                    hourly_rate,
                    spot_rate,
                    availability,
                    fetched_at,
                    raw_metadata: serde_json::to_value(&flavor).unwrap_or_default(),
                };

                offerings.push(offering);
            }
        }

        tracing::info!("Fetched {} offerings from Hyperstack", offerings.len());
        Ok(offerings)
    }

    async fn health_check(&self) -> Result<ProviderHealth> {
        match self.fetch_flavors().await {
            Ok(_) => Ok(ProviderHealth {
                provider: ProviderEnum::Hyperstack,
                is_healthy: true,
                last_success_at: Some(Utc::now()),
                last_error: None,
            }),
            Err(e) => Ok(ProviderHealth {
                provider: ProviderEnum::Hyperstack,
                is_healthy: false,
                last_success_at: None,
                last_error: Some(e.to_string()),
            }),
        }
    }
}

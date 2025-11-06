use super::normalize::{normalize_gpu_type, normalize_region, parse_gpu_description};
use super::types::InstanceTypesResponse;
use crate::error::{AggregatorError, Result};
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use crate::providers::Provider;
use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use rust_decimal::Decimal;
use std::time::Duration;

pub struct LambdaProvider {
    client: Client,
    api_key: String,
    base_url: String,
}

impl LambdaProvider {
    pub fn new(api_key: String, base_url: String, timeout_seconds: u64) -> Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(timeout_seconds))
            .build()
            .map_err(|e| AggregatorError::Provider {
                provider: "lambda".to_string(),
                message: format!("Failed to create HTTP client: {}", e),
            })?;

        Ok(Self {
            client,
            api_key,
            base_url,
        })
    }

    async fn fetch_instance_types(&self) -> Result<InstanceTypesResponse> {
        let url = format!("{}/instance-types", self.base_url);

        tracing::debug!("Fetching instance types from Lambda: {}", url);

        let response = self
            .client
            .get(&url)
            .basic_auth(&self.api_key, Some("")) // Basic Auth: username=api_key, password=empty
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "lambda".to_string(),
                message: format!("Failed to fetch instance types: {}", e),
            })?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            tracing::error!("Lambda API returned error: {} - {}", status, error_text);
            return Err(AggregatorError::Provider {
                provider: "lambda".to_string(),
                message: format!("API returned status: {} - {}", status, error_text),
            });
        }

        let instance_types: InstanceTypesResponse =
            response
                .json()
                .await
                .map_err(|e| AggregatorError::Provider {
                    provider: "lambda".to_string(),
                    message: format!("Failed to parse instance types: {}", e),
                })?;

        Ok(instance_types)
    }
}

#[async_trait]
impl Provider for LambdaProvider {
    fn provider_id(&self) -> ProviderEnum {
        ProviderEnum::Lambda
    }

    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>> {
        let instance_types = self.fetch_instance_types().await?;

        let fetched_at = Utc::now();
        let mut offerings = Vec::new();

        for (instance_name, wrapper) in instance_types {
            let instance_type = wrapper.instance_type;

            // Parse GPU information from description string
            let gpu_info = match parse_gpu_description(&instance_type.description) {
                Some(info) => info,
                None => {
                    tracing::warn!(
                        "Failed to parse GPU description for {}: {}",
                        instance_name,
                        instance_type.description
                    );
                    continue;
                }
            };

            // Normalize GPU type
            let gpu_type = normalize_gpu_type(&gpu_info.model);

            // Convert price from cents to dollars
            let hourly_rate = Decimal::from(instance_type.price_cents_per_hour) / Decimal::from(100);

            // Determine region - use first available or "global"
            let region = wrapper
                .regions_with_capacity_available
                .first()
                .map(|r| normalize_region(&r.name))
                .unwrap_or_else(|| "global".to_string());

            // Check availability - true if any regions have capacity
            let availability = !wrapper.regions_with_capacity_available.is_empty();

            // Create offering
            let offering = GpuOffering {
                id: instance_type.name.clone(),
                provider: ProviderEnum::Lambda,
                gpu_type,
                gpu_memory_gb: gpu_info.memory_gb,
                gpu_count: gpu_info.count,
                system_memory_gb: instance_type.specs.memory_gib,
                vcpu_count: instance_type.specs.vcpus,
                region,
                hourly_rate,
                spot_rate: None, // Lambda doesn't provide spot pricing in this endpoint
                availability,
                fetched_at,
                raw_metadata: serde_json::to_value(&instance_type).unwrap_or_default(),
            };

            offerings.push(offering);
        }

        tracing::info!("Fetched {} offerings from Lambda", offerings.len());
        Ok(offerings)
    }

    async fn health_check(&self) -> Result<ProviderHealth> {
        match self.fetch_instance_types().await {
            Ok(_) => Ok(ProviderHealth {
                provider: ProviderEnum::Lambda,
                is_healthy: true,
                last_success_at: Some(Utc::now()),
                last_error: None,
            }),
            Err(e) => Ok(ProviderHealth {
                provider: ProviderEnum::Lambda,
                is_healthy: false,
                last_success_at: None,
                last_error: Some(e.to_string()),
            }),
        }
    }
}

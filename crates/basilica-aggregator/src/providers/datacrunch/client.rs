use super::normalize::{normalize_gpu_type, parse_interconnect};
use super::types::{InstanceType, Location, LocationAvailability};
use crate::error::{AggregatorError, Result};
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use crate::providers::http_utils::{handle_error_response, HttpClientBuilder};
use crate::providers::Provider;
use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::RwLock;

#[derive(Debug, Clone, Serialize)]
struct TokenRequest {
    grant_type: String,
    client_id: String,
    client_secret: String,
}

#[derive(Debug, Clone, Deserialize)]
struct TokenResponse {
    access_token: String,
    #[allow(dead_code)]
    refresh_token: String,
    expires_in: u64,
    #[allow(dead_code)]
    token_type: String,
    #[serde(default)]
    #[allow(dead_code)]
    scope: String,
}

#[derive(Debug, Clone)]
struct TokenCache {
    access_token: String,
    expires_at: chrono::DateTime<Utc>,
}

impl TokenCache {
    fn new(access_token: String, expires_in: u64) -> Self {
        let expires_at = Utc::now() + chrono::Duration::seconds(expires_in as i64);
        Self {
            access_token,
            expires_at,
        }
    }

    fn is_expired(&self) -> bool {
        // Consider token expired 60 seconds before actual expiration
        Utc::now() >= self.expires_at - chrono::Duration::seconds(60)
    }
}

pub struct DataCrunchProvider {
    client: Client,
    client_id: String,
    client_secret: String,
    base_url: String,
    token_cache: Arc<RwLock<Option<TokenCache>>>,
}

impl DataCrunchProvider {
    pub fn new(
        client_id: String,
        client_secret: String,
        base_url: String,
        timeout_seconds: u64,
    ) -> Result<Self> {
        let client = HttpClientBuilder::new(timeout_seconds).build("datacrunch")?;

        Ok(Self {
            client,
            client_id,
            client_secret,
            base_url,
            token_cache: Arc::new(RwLock::new(None)),
        })
    }

    async fn get_access_token(&self) -> Result<String> {
        // Check if we have a valid cached token
        {
            let token_read = self.token_cache.read().await;
            if let Some(cached) = token_read.as_ref() {
                if !cached.is_expired() {
                    return Ok(cached.access_token.clone());
                }
            }
        }

        // Token is missing or expired, acquire write lock to refresh
        let mut token_write = self.token_cache.write().await;

        // Double-check after acquiring write lock (another task might have refreshed)
        if let Some(cached) = token_write.as_ref() {
            if !cached.is_expired() {
                return Ok(cached.access_token.clone());
            }
        }

        // Fetch new token using DataCrunch OAuth2 endpoint (JSON format)
        let token_url = format!("{}/oauth2/token", self.base_url);
        let request_body = TokenRequest {
            grant_type: "client_credentials".to_string(),
            client_id: self.client_id.clone(),
            client_secret: self.client_secret.clone(),
        };

        tracing::debug!("Fetching OAuth2 token from DataCrunch");

        let response = self
            .client
            .post(&token_url)
            .json(&request_body)
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("Failed to send OAuth2 token request: {}", e),
            })?;

        let response = handle_error_response(response, "datacrunch").await?;

        let token_response: TokenResponse = response.json().await.map_err(|e| {
            tracing::error!("Failed to parse OAuth2 token response: {}", e);
            AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("Failed to parse OAuth2 token response: {}", e),
            }
        })?;

        tracing::info!("Successfully obtained OAuth2 token from DataCrunch");

        let access_token = token_response.access_token.clone();
        let expires_in = token_response.expires_in;

        // Cache the token
        *token_write = Some(TokenCache::new(access_token.clone(), expires_in));

        Ok(access_token)
    }

    async fn fetch_instance_types(&self) -> Result<Vec<InstanceType>> {
        let url = format!("{}/instance-types?currency=usd", self.base_url);
        let access_token = self.get_access_token().await?;

        let response = self
            .client
            .get(&url)
            .bearer_auth(&access_token)
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("Failed to fetch instance types: {}", e),
            })?;

        let response = handle_error_response(response, "datacrunch").await?;

        let instance_types: Vec<InstanceType> =
            response
                .json()
                .await
                .map_err(|e| AggregatorError::Provider {
                    provider: "datacrunch".to_string(),
                    message: format!("Failed to parse instance types: {}", e),
                })?;

        Ok(instance_types)
    }

    async fn fetch_locations(&self) -> Result<Vec<Location>> {
        let url = format!("{}/locations", self.base_url);
        let access_token = self.get_access_token().await?;

        let response = self
            .client
            .get(&url)
            .bearer_auth(&access_token)
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("Failed to fetch locations: {}", e),
            })?;

        let response = handle_error_response(response, "datacrunch").await?;

        let locations: Vec<Location> =
            response
                .json()
                .await
                .map_err(|e| AggregatorError::Provider {
                    provider: "datacrunch".to_string(),
                    message: format!("Failed to parse locations: {}", e),
                })?;

        Ok(locations)
    }

    async fn fetch_availability(&self) -> Result<Vec<LocationAvailability>> {
        let url = format!("{}/instance-availability", self.base_url);
        let access_token = self.get_access_token().await?;

        let response = self
            .client
            .get(&url)
            .bearer_auth(&access_token)
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("Failed to fetch availability: {}", e),
            })?;

        let response = handle_error_response(response, "datacrunch").await?;

        let availability: Vec<LocationAvailability> =
            response
                .json()
                .await
                .map_err(|e| AggregatorError::Provider {
                    provider: "datacrunch".to_string(),
                    message: format!("Failed to parse availability: {}", e),
                })?;

        Ok(availability)
    }
}

#[async_trait]
impl Provider for DataCrunchProvider {
    fn provider_id(&self) -> ProviderEnum {
        ProviderEnum::DataCrunch
    }

    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>> {
        let instance_types = self.fetch_instance_types().await?;

        // Try to fetch locations and availability data
        // If either fails, fall back to single global offering per instance type
        let locations_result = self.fetch_locations().await;
        let availability_result = self.fetch_availability().await;

        let fetched_at = Utc::now();
        let mut offerings = Vec::new();

        match (&locations_result, &availability_result) {
            (Ok(locations), Ok(availability_data)) if !locations.is_empty() => {
                // We have location and availability data - create per-region offerings
                tracing::info!(
                    "Using per-region data for DataCrunch ({} locations)",
                    locations.len()
                );

                // Build a map of (location_code, instance_type) -> bool (available)
                // The API returns: location_code -> [available_instance_types]
                let mut availability_map = std::collections::HashMap::new();
                for location_avail in availability_data.iter() {
                    for instance_type_id in &location_avail.availabilities {
                        availability_map.insert(
                            (
                                location_avail.location_code.clone(),
                                instance_type_id.clone(),
                            ),
                            true,
                        );
                    }
                }

                for instance_type in &instance_types {
                    let gpu_model = instance_type
                        .model
                        .as_ref()
                        .unwrap_or(&instance_type.gpu.description);
                    let gpu_type = normalize_gpu_type(gpu_model);
                    let hourly_rate = instance_type
                        .price_per_hour
                        .parse::<Decimal>()
                        .unwrap_or_default();
                    let interconnect = parse_interconnect(&instance_type.gpu.description);
                    let storage = Some(instance_type.storage.description.clone());

                    // Create one offering per location
                    for location in locations.iter() {
                        // Check if this instance type is available at this location
                        // The key is (location_code, instance_type_id)
                        let is_available = availability_map
                            .get(&(location.code.clone(), instance_type.instance_type.clone()))
                            .copied()
                            .unwrap_or(false);

                        let offering_id = format!("{}-{}", instance_type.id, location.code);

                        let offering = GpuOffering {
                            id: offering_id,
                            provider: ProviderEnum::DataCrunch,
                            gpu_type: gpu_type.clone(),
                            gpu_memory_gb: Some(instance_type.gpu_memory.size_in_gigabytes),
                            gpu_count: instance_type.gpu.number_of_gpus,
                            interconnect: interconnect.clone(),
                            storage: storage.clone(),
                            deployment_type: Some("vm".to_string()),
                            system_memory_gb: instance_type.memory.size_in_gigabytes,
                            vcpu_count: instance_type.cpu.number_of_cores,
                            region: location.code.clone(),
                            hourly_rate,
                            availability: is_available,
                            fetched_at,
                            raw_metadata: serde_json::to_value(instance_type).unwrap_or_default(),
                        };

                        offerings.push(offering);
                    }
                }

                tracing::info!(
                    "Fetched {} offerings from DataCrunch ({} instance types × {} locations)",
                    offerings.len(),
                    instance_types.len(),
                    locations.len()
                );
            }
            _ => {
                // Fall back to global offerings (no per-region data available)
                tracing::warn!(
                    "Unable to fetch location/availability data from DataCrunch, using global region. \
                     Locations error: {:?}, Availability error: {:?}",
                    locations_result.err(),
                    availability_result.err()
                );

                for instance_type in &instance_types {
                    let gpu_model = instance_type
                        .model
                        .as_ref()
                        .unwrap_or(&instance_type.gpu.description);
                    let gpu_type = normalize_gpu_type(gpu_model);
                    let hourly_rate = instance_type
                        .price_per_hour
                        .parse::<Decimal>()
                        .unwrap_or_default();
                    let interconnect = parse_interconnect(&instance_type.gpu.description);
                    let storage = Some(instance_type.storage.description.clone());

                    let offering = GpuOffering {
                        id: instance_type.id.clone(),
                        provider: ProviderEnum::DataCrunch,
                        gpu_type,
                        gpu_memory_gb: Some(instance_type.gpu_memory.size_in_gigabytes),
                        gpu_count: instance_type.gpu.number_of_gpus,
                        interconnect,
                        storage,
                        deployment_type: Some("vm".to_string()),
                        system_memory_gb: instance_type.memory.size_in_gigabytes,
                        vcpu_count: instance_type.cpu.number_of_cores,
                        region: "global".to_string(),
                        hourly_rate,
                        availability: true,
                        fetched_at,
                        raw_metadata: serde_json::to_value(instance_type).unwrap_or_default(),
                    };

                    offerings.push(offering);
                }

                tracing::info!(
                    "Fetched {} offerings from DataCrunch (global region fallback)",
                    offerings.len()
                );
            }
        }

        Ok(offerings)
    }

    async fn health_check(&self) -> Result<ProviderHealth> {
        match self.fetch_instance_types().await {
            Ok(_) => Ok(ProviderHealth {
                provider: ProviderEnum::DataCrunch,
                is_healthy: true,
                last_success_at: Some(Utc::now()),
                last_error: None,
            }),
            Err(e) => Ok(ProviderHealth {
                provider: ProviderEnum::DataCrunch,
                is_healthy: false,
                last_success_at: None,
                last_error: Some(e.to_string()),
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_datacrunch_h100_instance() {
        // Sample H100 instance from DataCrunch API (1x H100 SXM5 80GB)
        let json_data = r#"{
            "best_for": ["Gargantuan ML models", "Multi-GPU training", "FP64 HPC", "NVLINK"],
            "cpu": {
                "description": "30 CPU",
                "number_of_cores": 30
            },
            "deploy_warning": "H100: Use Nvidia driver 535 or higher for best performance",
            "description": "Dedicated Hardware Instance",
            "gpu": {
                "description": "1x H100 SXM5 80GB",
                "number_of_gpus": 1
            },
            "gpu_memory": {
                "description": "80GB GPU RAM",
                "size_in_gigabytes": 80
            },
            "id": "c01dd00d-0000-480b-ae4e-d429115d055b",
            "instance_type": "1H100.80S.30V",
            "memory": {
                "description": "120GB RAM",
                "size_in_gigabytes": 120
            },
            "model": "H100",
            "name": "H100 SXM5 80GB",
            "p2p": null,
            "price_per_hour": "1.99",
            "dynamic_price": "1.98",
            "max_dynamic_price": "2.99",
            "serverless_price": "2.19",
            "storage": {
                "description": "dynamic"
            },
            "currency": "usd",
            "manufacturer": "NVIDIA",
            "display_name": null
        }"#;

        // Parse the JSON
        let instance: InstanceType = serde_json::from_str(json_data).expect("Failed to parse JSON");

        // Verify the parsed data
        assert_eq!(instance.id, "c01dd00d-0000-480b-ae4e-d429115d055b");
        assert_eq!(instance.instance_type, "1H100.80S.30V");
        assert_eq!(instance.model, Some("H100".to_string()));
        assert_eq!(instance.price_per_hour, "1.99");

        // Verify CPU specs
        assert_eq!(instance.cpu.number_of_cores, 30);
        assert_eq!(instance.cpu.description, "30 CPU");

        // Verify GPU specs
        assert_eq!(instance.gpu.number_of_gpus, 1);
        assert_eq!(instance.gpu.description, "1x H100 SXM5 80GB");

        // Verify memory specs
        assert_eq!(instance.memory.size_in_gigabytes, 120);
        assert_eq!(instance.gpu_memory.size_in_gigabytes, 80);

        // Verify storage
        assert_eq!(instance.storage.description, "dynamic");

        // Print the parsed data
        println!("Successfully parsed DataCrunch H100 instance:");
        println!("  ID: {}", instance.id);
        println!("  Instance Type: {}", instance.instance_type);
        println!("  GPU Model: {}", instance.model.as_ref().unwrap());
        println!("  GPU Count: {}", instance.gpu.number_of_gpus);
        println!("  GPU Memory: {}GB", instance.gpu_memory.size_in_gigabytes);
        println!("  vCPUs: {}", instance.cpu.number_of_cores);
        println!("  System Memory: {}GB", instance.memory.size_in_gigabytes);
        println!("  Price/hour: ${}", instance.price_per_hour);
    }
}

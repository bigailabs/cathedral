use super::normalize::normalize_gpu_type;
use super::types::{InstanceAvailability, InstanceType, Location};
use crate::error::{AggregatorError, Result};
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use crate::providers::Provider;
use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Duration;
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
        let client = Client::builder()
            .timeout(Duration::from_secs(timeout_seconds))
            .build()
            .map_err(|e| AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("Failed to create HTTP client: {}", e),
            })?;

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

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            tracing::error!("OAuth2 token request failed: {} - {}", status, error_text);
            return Err(AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("OAuth2 token request failed: {} - {}", status, error_text),
            });
        }

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

        if !response.status().is_success() {
            return Err(AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("API returned status: {}", response.status()),
            });
        }

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

    #[allow(dead_code)]
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

        if !response.status().is_success() {
            return Err(AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("API returned status: {}", response.status()),
            });
        }

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

    #[allow(dead_code)]
    async fn fetch_availability(&self) -> Result<Vec<InstanceAvailability>> {
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

        if !response.status().is_success() {
            return Err(AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: format!("API returned status: {}", response.status()),
            });
        }

        let availability: Vec<InstanceAvailability> =
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

        let fetched_at = Utc::now();
        let mut offerings = Vec::new();

        for instance_type in instance_types {
            // Normalize GPU type - use the model field or parse from description
            let gpu_model = instance_type
                .model
                .as_ref()
                .unwrap_or(&instance_type.gpu.description);

            let gpu_type = normalize_gpu_type(gpu_model);

            // Parse price strings to Decimal
            let hourly_rate = instance_type
                .price_per_hour
                .parse::<Decimal>()
                .unwrap_or_default();

            let spot_rate = instance_type
                .spot_price
                .as_ref()
                .and_then(|s| s.parse::<Decimal>().ok());

            // Create single offering per instance type (simplified - no location/availability data)
            // DataCrunch's locations/availability endpoints require different permissions
            let offering = GpuOffering {
                id: instance_type.id.clone(),
                provider: ProviderEnum::DataCrunch,
                gpu_type,
                gpu_memory_gb: instance_type.gpu_memory.size_in_gigabytes,
                gpu_count: instance_type.gpu.number_of_gpus,
                system_memory_gb: instance_type.memory.size_in_gigabytes,
                vcpu_count: instance_type.cpu.number_of_cores,
                region: "global".to_string(), // Simplified: use global since we can't fetch locations
                hourly_rate,
                spot_rate,
                availability: true, // Assume available if listed in API
                fetched_at,
                raw_metadata: serde_json::to_value(&instance_type).unwrap_or_default(),
            };

            offerings.push(offering);
        }

        tracing::info!("Fetched {} offerings from DataCrunch", offerings.len());
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

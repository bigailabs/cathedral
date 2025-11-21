use super::normalize::{normalize_gpu_type, normalize_region, parse_gpu_description};
use super::types::InstanceTypesResponse;
use crate::error::{AggregatorError, Result};
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use crate::providers::http_utils::{handle_error_response, HttpClientBuilder};
use crate::providers::Provider;
use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use rust_decimal::Decimal;

pub struct LambdaProvider {
    client: Client,
    api_key: String,
    base_url: String,
}

impl LambdaProvider {
    pub fn new(api_key: String) -> Result<Self> {
        let client =
            HttpClientBuilder::new(crate::providers::DEFAULT_TIMEOUT_SECONDS).build("lambda")?;

        Ok(Self {
            client,
            api_key,
            base_url: crate::providers::LAMBDA_API_BASE_URL.to_string(),
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

        let response = handle_error_response(response, "lambda").await?;

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

            // Convert price from cents to dollars and normalize to per-GPU rate
            // Lambda provides total instance price, so divide by gpu_count
            let hourly_rate_per_gpu = Decimal::from(instance_type.price_cents_per_hour)
                / Decimal::from(100)
                / Decimal::from(gpu_info.count.max(1));

            // Extract storage information (in GiB)
            let storage = Some(instance_type.specs.storage_gib.to_string());

            // Create one offering per region with capacity
            if wrapper.regions_with_capacity_available.is_empty() {
                // If no regions available, create single "global" offering as unavailable
                let offering = GpuOffering {
                    id: instance_type.name.clone(),
                    provider: ProviderEnum::Lambda,
                    gpu_type: gpu_type.clone(),
                    gpu_memory_gb_per_gpu: Some(gpu_info.memory_gb),
                    gpu_count: gpu_info.count,
                    interconnect: None,
                    storage: storage.clone(),
                    deployment_type: None,
                    system_memory_gb: instance_type.specs.memory_gib,
                    vcpu_count: instance_type.specs.vcpus,
                    region: "global".to_string(),
                    hourly_rate_per_gpu,
                    availability: false,
                    fetched_at,
                    raw_metadata: serde_json::to_value(&instance_type).unwrap_or_default(),
                };
                offerings.push(offering);
            } else {
                // Create one offering per available region
                for region_info in &wrapper.regions_with_capacity_available {
                    let region = normalize_region(&region_info.name);
                    let offering_id = format!("{}-{}", instance_type.name, region);

                    let offering = GpuOffering {
                        id: offering_id,
                        provider: ProviderEnum::Lambda,
                        gpu_type: gpu_type.clone(),
                        gpu_memory_gb_per_gpu: Some(gpu_info.memory_gb),
                        gpu_count: gpu_info.count,
                        interconnect: None, // Lambda API doesn't provide interconnect info
                        storage: storage.clone(),
                        deployment_type: None, // Set as NULL for now
                        system_memory_gb: instance_type.specs.memory_gib,
                        vcpu_count: instance_type.specs.vcpus,
                        region,
                        hourly_rate_per_gpu,
                        availability: true, // True since this region has capacity
                        fetched_at,
                        raw_metadata: serde_json::to_value(&instance_type).unwrap_or_default(),
                    };
                    offerings.push(offering);
                }
            }
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

    async fn create_ssh_key(
        &self,
        _name: String,
        _public_key: String,
    ) -> Result<crate::providers::ProviderSshKey> {
        Err(AggregatorError::Provider {
            provider: "lambda".to_string(),
            message: "SSH key management not yet implemented for Lambda".to_string(),
        })
    }

    async fn list_ssh_keys(&self) -> Result<Vec<crate::providers::ProviderSshKey>> {
        Err(AggregatorError::Provider {
            provider: "lambda".to_string(),
            message: "SSH key management not yet implemented for Lambda".to_string(),
        })
    }

    async fn delete_ssh_key(&self, _provider_key_id: &str) -> Result<()> {
        Err(AggregatorError::Provider {
            provider: "lambda".to_string(),
            message: "SSH key management not yet implemented for Lambda".to_string(),
        })
    }

    async fn deploy(
        &self,
        _request: crate::providers::DeployRequest,
    ) -> Result<crate::providers::ProviderDeployment> {
        Err(AggregatorError::Provider {
            provider: "lambda".to_string(),
            message: "Deployment not yet implemented for Lambda".to_string(),
        })
    }

    async fn get_deployment(
        &self,
        _instance_id: &str,
    ) -> Result<crate::providers::ProviderDeployment> {
        Err(AggregatorError::Provider {
            provider: "lambda".to_string(),
            message: "Get deployment not yet implemented for Lambda".to_string(),
        })
    }

    async fn delete_deployment(&self, _instance_id: &str) -> Result<()> {
        Err(AggregatorError::Provider {
            provider: "lambda".to_string(),
            message: "Delete deployment not yet implemented for Lambda".to_string(),
        })
    }
}

#[cfg(test)]
mod tests {

    use crate::providers::lambda::types::InstanceTypeWrapper;

    #[test]
    fn test_parse_lambda_a100_instance() {
        // Sample A100 instance wrapper from Lambda instance-types API
        let json_data = r#"{
            "instance_type": {
                "name": "gpu_1x_a100",
                "price_cents_per_hour": 110,
                "description": "1x NVIDIA A100 (40 GB SXM4)",
                "specs": {
                    "vcpus": 30,
                    "memory_gib": 200,
                    "storage_gib": 1400
                }
            },
            "regions_with_capacity_available": [
                {
                    "name": "us-west-2",
                    "description": "California, USA"
                },
                {
                    "name": "us-east-1",
                    "description": "Virginia, USA"
                }
            ]
        }"#;

        // Parse the JSON
        let wrapper: InstanceTypeWrapper =
            serde_json::from_str(json_data).expect("Failed to parse JSON");

        // Verify instance type data
        assert_eq!(wrapper.instance_type.name, "gpu_1x_a100");
        assert_eq!(wrapper.instance_type.price_cents_per_hour, 110);
        assert_eq!(
            wrapper.instance_type.description,
            "1x NVIDIA A100 (40 GB SXM4)"
        );

        // Verify specs
        assert_eq!(wrapper.instance_type.specs.vcpus, 30);
        assert_eq!(wrapper.instance_type.specs.memory_gib, 200);
        assert_eq!(wrapper.instance_type.specs.storage_gib, 1400);

        // Verify regions
        assert_eq!(wrapper.regions_with_capacity_available.len(), 2);
        assert_eq!(wrapper.regions_with_capacity_available[0].name, "us-west-2");
        assert_eq!(
            wrapper.regions_with_capacity_available[0].description,
            "California, USA"
        );
        assert_eq!(wrapper.regions_with_capacity_available[1].name, "us-east-1");
        assert_eq!(
            wrapper.regions_with_capacity_available[1].description,
            "Virginia, USA"
        );

        // Print the parsed data
        println!("Successfully parsed Lambda A100 instance:");
        println!("  Name: {}", wrapper.instance_type.name);
        println!("  Description: {}", wrapper.instance_type.description);
        println!(
            "  Price: ${:.2}/hour",
            wrapper.instance_type.price_cents_per_hour as f64 / 100.0
        );
        println!("  vCPUs: {}", wrapper.instance_type.specs.vcpus);
        println!("  Memory: {}GB", wrapper.instance_type.specs.memory_gib);
        println!("  Storage: {}GB", wrapper.instance_type.specs.storage_gib);
        println!(
            "  Available Regions: {}",
            wrapper.regions_with_capacity_available.len()
        );
        for region in &wrapper.regions_with_capacity_available {
            println!("    - {} ({})", region.name, region.description);
        }
    }
}

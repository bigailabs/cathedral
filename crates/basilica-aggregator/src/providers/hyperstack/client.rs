use super::normalize::{
    normalize_gpu_type, normalize_region, parse_gpu_memory, parse_interconnect,
};
use super::types::{
    CreateKeypairRequest, CreateKeypairResponse, DeployVmRequest, DeployVmResponse,
    FlavorsResponse, GetVmResponse, Keypair, PricebookResponse, SecurityRuleRequest,
    VirtualMachine,
};
use crate::error::{AggregatorError, Result};
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use crate::providers::http_utils::{handle_error_response, HttpClientBuilder};
use crate::providers::Provider;
use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use rust_decimal::Decimal;
use std::collections::HashMap;

pub struct HyperstackProvider {
    client: Client,
    api_key: String,
    base_url: String,
}

impl HyperstackProvider {
    pub fn new(api_key: String) -> Result<Self> {
        let client = HttpClientBuilder::new(crate::providers::DEFAULT_TIMEOUT_SECONDS)
            .build("hyperstack")?;

        Ok(Self {
            client,
            api_key,
            base_url: crate::providers::HYPERSTACK_API_BASE_URL.to_string(),
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

        let response = handle_error_response(response, "hyperstack").await?;

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

    async fn fetch_pricebook(&self) -> Result<HashMap<String, Decimal>> {
        let url = format!("{}/pricebook", self.base_url);

        tracing::debug!("Fetching pricebook from Hyperstack: {}", url);

        let response = self
            .client
            .get(&url)
            .header("api_key", &self.api_key)
            .header("accept", "application/json")
            .send()
            .await
            .map_err(|e| {
                tracing::warn!("Failed to fetch Hyperstack pricebook: {}", e);
                AggregatorError::Provider {
                    provider: "hyperstack".to_string(),
                    message: format!("Failed to fetch pricebook: {}", e),
                }
            })?;

        let response = handle_error_response(response, "hyperstack").await?;

        let pricebook: PricebookResponse = response.json().await.map_err(|e| {
            tracing::warn!("Failed to parse Hyperstack pricebook response: {}", e);
            AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to parse pricebook response: {}", e),
            }
        })?;

        // Build HashMap mapping GPU model name to hourly price
        let mut price_map = HashMap::new();
        for item in pricebook {
            // Use the actual price (after discounts) not the original value
            // Parse string value to Decimal
            let price = item.value.parse::<Decimal>().unwrap_or_else(|e| {
                tracing::warn!(
                    "Failed to parse price '{}' for {}: {}",
                    item.value,
                    item.name,
                    e
                );
                Decimal::ZERO
            });
            price_map.insert(item.name.clone(), price);

            tracing::debug!(
                "Pricebook entry: {} = ${}/hr{}",
                item.name,
                item.value,
                if item.discount_applied {
                    " (discounted)"
                } else {
                    ""
                }
            );
        }

        tracing::info!(
            "Loaded {} price entries from Hyperstack pricebook",
            price_map.len()
        );

        Ok(price_map)
    }

    // ========================================================================
    // SSH Key Management
    // ========================================================================

    /// Create a new SSH keypair
    pub async fn create_keypair_impl(
        &self,
        name: String,
        environment_name: String,
        public_key: String,
    ) -> Result<Keypair> {
        let url = format!("{}/core/keypairs", self.base_url);

        let request_body = CreateKeypairRequest {
            name: name.clone(),
            environment_name: environment_name.clone(),
            public_key: public_key.clone(),
        };

        tracing::info!(
            "Creating SSH keypair in Hyperstack: name='{}', environment='{}'",
            name,
            environment_name
        );

        tracing::debug!(
            "Hyperstack keypair request: url='{}', name='{}', environment='{}', public_key=<REDACTED>",
            url,
            name,
            environment_name
        );

        let response = self
            .client
            .post(&url)
            .header("api_key", &self.api_key)
            .header("Content-Type", "application/json")
            .json(&request_body)
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to create keypair: {}", e),
            })?;

        let response = handle_error_response(response, "hyperstack").await?;

        let create_response: CreateKeypairResponse =
            response
                .json()
                .await
                .map_err(|e| AggregatorError::Provider {
                    provider: "hyperstack".to_string(),
                    message: format!("Failed to parse keypair response: {}", e),
                })?;

        if !create_response.status {
            tracing::error!(
                "Hyperstack keypair creation failed: {}",
                create_response.message
            );
            return Err(AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to create keypair: {}", create_response.message),
            });
        }

        tracing::info!(
            "✓ Successfully created SSH keypair in Hyperstack: id={}, name='{}', environment='{}', fingerprint='{}'",
            create_response.keypair.id,
            create_response.keypair.name,
            environment_name,
            create_response.keypair.fingerprint
        );

        Ok(create_response.keypair)
    }

    /// Delete an SSH keypair
    pub async fn delete_keypair(&self, keypair_id: u32) -> Result<()> {
        let url = format!("{}/core/keypairs/{}", self.base_url, keypair_id);

        tracing::debug!("Deleting SSH keypair: {}", keypair_id);

        let response = self
            .client
            .delete(&url)
            .header("api_key", &self.api_key)
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to delete keypair: {}", e),
            })?;

        handle_error_response(response, "hyperstack").await?;

        tracing::info!("Successfully deleted SSH keypair: {}", keypair_id);

        Ok(())
    }

    // ========================================================================
    // Virtual Machine Deployment and Management
    // ========================================================================

    /// Deploy a new virtual machine
    pub async fn deploy_vm(&self, request: DeployVmRequest) -> Result<VirtualMachine> {
        let url = format!("{}/core/virtual-machines", self.base_url);

        tracing::debug!(
            "Deploying VM: {} with flavor {}",
            request.name,
            request.flavor_name
        );

        let response = self
            .client
            .post(&url)
            .header("api_key", &self.api_key)
            .header("Content-Type", "application/json")
            .json(&request)
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to deploy VM: {}", e),
            })?;

        let response = handle_error_response(response, "hyperstack").await?;

        let deploy_response: DeployVmResponse =
            response
                .json()
                .await
                .map_err(|e| AggregatorError::Provider {
                    provider: "hyperstack".to_string(),
                    message: format!("Failed to parse deploy VM response: {}", e),
                })?;

        if !deploy_response.status {
            return Err(AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to deploy VM: {}", deploy_response.message),
            });
        }

        let instance = deploy_response
            .instances
            .into_iter()
            .next()
            .ok_or_else(|| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: "No instance in deploy response".to_string(),
            })?;

        // Convert DeployVmInstance to VirtualMachine
        let vm: VirtualMachine = instance.into();

        tracing::info!("Successfully deployed VM: {}", vm.id);

        Ok(vm)
    }

    /// Get virtual machine details by ID
    pub async fn get_vm(&self, vm_id: u32) -> Result<VirtualMachine> {
        let url = format!("{}/core/virtual-machines/{}", self.base_url, vm_id);

        let response = self
            .client
            .get(&url)
            .header("api_key", &self.api_key)
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to get VM: {}", e),
            })?;

        let response = handle_error_response(response, "hyperstack").await?;

        // Get response body as text first for diagnostic logging
        let response_text = response
            .text()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to read VM response body: {}", e),
            })?;

        tracing::debug!(
            "Hyperstack get_vm({}) raw response: {}",
            vm_id,
            response_text
        );

        let vm_response: GetVmResponse =
            serde_json::from_str(&response_text).map_err(|e| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to parse VM response: {}", e),
            })?;

        if !vm_response.status {
            return Err(AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to get VM: {}", vm_response.message),
            });
        }

        Ok(vm_response.instance.into())
    }

    /// Delete a virtual machine
    pub async fn delete_vm(&self, vm_id: u32) -> Result<()> {
        let url = format!("{}/core/virtual-machines/{}", self.base_url, vm_id);

        tracing::debug!("Deleting VM: {}", vm_id);

        let response = self
            .client
            .delete(&url)
            .header("api_key", &self.api_key)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .send()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Failed to delete VM: {}", e),
            })?;

        handle_error_response(response, "hyperstack").await?;

        tracing::info!("Successfully deleted VM: {}", vm_id);

        Ok(())
    }
}

#[async_trait]
impl Provider for HyperstackProvider {
    fn provider_id(&self) -> ProviderEnum {
        ProviderEnum::Hyperstack
    }

    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>> {
        let flavors_response = self.fetch_flavors().await?;

        // Fetch pricebook for GPU pricing
        // If this fails, we'll continue with zero prices rather than failing completely
        let pricebook = self.fetch_pricebook().await.unwrap_or_else(|e| {
            tracing::warn!(
                "Failed to fetch Hyperstack pricebook, will use zero prices: {}",
                e
            );
            HashMap::new()
        });

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

            // Filter: Only process supported GPU types (A100, H100, B200)
            // Skip unsupported GPUs immediately
            if matches!(gpu_type, basilica_common::types::GpuCategory::Other(_)) {
                continue;
            }

            // Parse GPU memory from group's GPU string (e.g., "A100-80G-PCIe" -> 80)
            // If not found, we'll try parsing from individual flavor names as fallback
            let group_gpu_memory = parse_gpu_memory(&group.gpu);

            // Normalize region to "global" (consistent with DataCrunch)
            let region = normalize_region(&group.region_name);

            // Iterate through flavors in this group
            for flavor in group.flavors {
                // Skip flavors with no GPUs
                if flavor.gpu_count == 0 {
                    continue;
                }

                // Parse GPU memory from group GPU string, falling back to flavor name
                // For supported GPUs, store as NULL if we can't parse memory
                let gpu_memory_gb_per_gpu = group_gpu_memory
                    .or_else(|| parse_gpu_memory(&flavor.name))
                    .or_else(|| parse_gpu_memory(&flavor.gpu));

                // Log when we can't determine memory for supported GPUs
                if gpu_memory_gb_per_gpu.is_none() {
                    tracing::debug!(
                        "Unable to parse GPU memory from group GPU: '{}', flavor name: '{}', or flavor GPU: '{}' for supported GPU type {:?}. Storing with NULL memory.",
                        group.gpu,
                        flavor.name,
                        flavor.gpu,
                        gpu_type
                    );
                }

                // Convert RAM from float GB to u32
                let system_memory_gb = flavor.ram.round() as u32;

                // Get per-GPU hourly rate from pricebook
                // (vCPU, RAM, and storage are free for GPU flavors)
                // Note: This is the per-GPU price, billing multiplies by gpu_count
                let hourly_rate_per_gpu = pricebook.get(&group.gpu).copied().unwrap_or_else(|| {
                    tracing::warn!(
                        "No pricing found in pricebook for GPU model '{}', using $0",
                        group.gpu
                    );
                    Decimal::ZERO
                });

                // Use stock_available from flavor
                let availability = flavor.stock_available;

                // Parse interconnect from GPU string (e.g., "H100-80G-PCIe", "A100-80G-SXM4")
                let interconnect = parse_interconnect(&group.gpu);

                // Extract storage information (disk + ephemeral)
                let total_storage = flavor.disk + flavor.ephemeral.unwrap_or(0);
                let storage = Some(total_storage.to_string());

                // Create offering with unique ID using flavor ID
                let offering = GpuOffering {
                    id: format!("hyperstack-{}", flavor.id),
                    provider: ProviderEnum::Hyperstack,
                    gpu_type: gpu_type.clone(),
                    gpu_memory_gb_per_gpu,
                    gpu_count: flavor.gpu_count,
                    interconnect,
                    storage,
                    deployment_type: None, // Set as NULL for now
                    system_memory_gb,
                    vcpu_count: flavor.cpu,
                    region: region.clone(),
                    hourly_rate_per_gpu,
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

    async fn create_ssh_key(
        &self,
        name: String,
        public_key: String,
    ) -> Result<crate::providers::ProviderSshKey> {
        // Hyperstack requires an environment_name for SSH keys
        // Use from request if provided, otherwise default to "default"
        let environment_name = "default".to_string();

        let keypair = self
            .create_keypair_impl(name, environment_name, public_key)
            .await?;

        Ok(keypair.into())
    }

    async fn list_ssh_keys(&self) -> Result<Vec<crate::providers::ProviderSshKey>> {
        // Hyperstack doesn't have a list keypairs endpoint in the current implementation
        // Return empty list for now (can be implemented if API supports it)
        Ok(vec![])
    }

    async fn delete_ssh_key(&self, provider_key_id: &str) -> Result<()> {
        let keypair_id: u32 = provider_key_id
            .parse()
            .map_err(|_| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: format!("Invalid keypair ID format: {}", provider_key_id),
            })?;

        self.delete_keypair(keypair_id).await
    }

    async fn deploy(
        &self,
        request: crate::providers::DeployRequest,
    ) -> Result<crate::providers::ProviderDeployment> {
        // Get environment name from request or default
        let environment_name = request
            .environment_name
            .clone()
            .unwrap_or_else(|| "default".to_string());

        // Note: SSH key should already be registered with the provider via lazy registration
        // in the service layer. We just use the key_name from the request.
        tracing::info!(
            "Hyperstack VM deployment: environment='{}', key_name='{}', instance_type='{}'",
            environment_name,
            request.ssh_key_name,
            request.instance_type
        );

        // Get default image if not specified
        let image_name = request
            .image_name
            .clone()
            .unwrap_or_else(|| "Ubuntu Server 22.04 LTS R535 CUDA 12.2".to_string());

        // Create deployment request
        let deploy_request = DeployVmRequest {
            name: request.hostname.clone(),
            environment_name: environment_name.clone(),
            image_name,
            flavor_name: request.instance_type.clone(),
            key_name: request.ssh_key_name.clone(),
            user_data: None,
            assign_floating_ip: Some(true),
            count: Some(1),
            create_bootable_volume: None,
            security_rules: Some(vec![SecurityRuleRequest {
                direction: "ingress".to_string(),
                ethertype: "IPv4".to_string(),
                protocol: "tcp".to_string(),
                port_range_min: 22,
                port_range_max: 22,
                remote_ip_prefix: "0.0.0.0/0".to_string(),
            }]),
        };

        tracing::debug!(
            "Sending Hyperstack deployment request: {:?}",
            serde_json::to_value(&deploy_request).unwrap_or_default()
        );

        // Deploy VM
        let vm = self.deploy_vm(deploy_request).await?;

        Ok(vm.into())
    }

    async fn get_deployment(
        &self,
        instance_id: &str,
    ) -> Result<crate::providers::ProviderDeployment> {
        let vm_id: u32 = instance_id.parse().map_err(|_| AggregatorError::Provider {
            provider: "hyperstack".to_string(),
            message: format!("Invalid VM ID: {}", instance_id),
        })?;

        let vm = self.get_vm(vm_id).await?;
        Ok(vm.into())
    }

    async fn delete_deployment(&self, instance_id: &str) -> Result<()> {
        let vm_id: u32 = instance_id.parse().map_err(|_| AggregatorError::Provider {
            provider: "hyperstack".to_string(),
            message: format!("Invalid VM ID: {}", instance_id),
        })?;

        self.delete_vm(vm_id).await
    }
}

#[cfg(test)]
mod tests {

    use crate::providers::hyperstack::types::Flavor;

    #[test]
    fn test_parse_hyperstack_h100_flavor() {
        // Sample H100 flavor from Hyperstack API (1x H100 80GB PCIe)
        let json_data = r#"{
            "id": 95,
            "name": "n3-H100x1",
            "display_name": null,
            "region_name": "CANADA-1",
            "cpu": 28,
            "ram": 180.0,
            "disk": 100,
            "ephemeral": 750,
            "gpu": "H100-80G-PCIe",
            "gpu_count": 1,
            "stock_available": true,
            "created_at": "2024-04-18T15:19:56",
            "labels": [
                {
                    "id": 16717,
                    "label": "network-optimised"
                }
            ],
            "features": {
                "network_optimised": true,
                "no_hibernation": false,
                "no_snapshot": false,
                "local_storage_only": false
            }
        }"#;

        // Parse the JSON
        let flavor: Flavor = serde_json::from_str(json_data).expect("Failed to parse JSON");

        // Verify the parsed data
        assert_eq!(flavor.id, 95);
        assert_eq!(flavor.name, "n3-H100x1");
        assert_eq!(flavor.display_name, None);
        assert_eq!(flavor.region_name, "CANADA-1");

        // Verify compute specs
        assert_eq!(flavor.cpu, 28);
        assert_eq!(flavor.ram, 180.0);

        // Verify GPU specs
        assert_eq!(flavor.gpu, "H100-80G-PCIe");
        assert_eq!(flavor.gpu_count, 1);

        // Verify storage
        assert_eq!(flavor.disk, 100);
        assert_eq!(flavor.ephemeral, Some(750));

        // Verify availability
        assert!(flavor.stock_available);

        // Verify features
        assert!(flavor.features.network_optimised);
        assert!(!flavor.features.no_hibernation);
        assert!(!flavor.features.no_snapshot);
        assert!(!flavor.features.local_storage_only);

        // Verify labels
        assert_eq!(flavor.labels.len(), 1);
        assert_eq!(flavor.labels[0].label, "network-optimised");

        // Print the parsed data
        println!("Successfully parsed Hyperstack H100 flavor:");
        println!("  ID: {}", flavor.id);
        println!("  Name: {}", flavor.name);
        println!("  Region: {}", flavor.region_name);
        println!("  GPU Model: {}", flavor.gpu);
        println!("  GPU Count: {}", flavor.gpu_count);
        println!("  vCPUs: {}", flavor.cpu);
        println!("  RAM: {}GB", flavor.ram);
        println!("  Disk: {}GB", flavor.disk);
        println!("  Ephemeral Storage: {}GB", flavor.ephemeral.unwrap_or(0));
        println!("  Stock Available: {}", flavor.stock_available);
        println!("  Network Optimised: {}", flavor.features.network_optimised);
    }
}

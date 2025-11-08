use super::normalize::{format_storage, get_gpu_memory, normalize_gpu_type, normalize_region};
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
        let url = format!("{}/inventory", self.base_url);

        tracing::debug!("Fetching listings from HydraHost: {}", url);

        let response = self
            .client
            .get(&url)
            .header("x-api-key", &self.api_key) // HydraHost uses x-api-key header
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

        // Get response text for better error logging
        let response_text = response
            .text()
            .await
            .map_err(|e| AggregatorError::Provider {
                provider: "hydrahost".to_string(),
                message: format!("Failed to read response body: {}", e),
            })?;

        // Try to parse as JSON
        let listings_response: ListingsResponse =
            serde_json::from_str(&response_text).map_err(|e| {
                tracing::error!("Serde error details: {}", e);
                tracing::debug!(
                    "Response text (first 2000 chars): {}",
                    &response_text[..response_text.len().min(2000)]
                );
                AggregatorError::Provider {
                    provider: "hydrahost".to_string(),
                    message: format!(
                        "Failed to parse listings response: {} - Column: {}, Line: {}",
                        e,
                        e.column(),
                        e.line()
                    ),
                }
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
            // Skip listings with no GPUs (CPU-only machines)
            let gpu_count = listing.specs.gpu.count.unwrap_or(0);
            if gpu_count == 0 {
                continue;
            }

            // Get GPU model - either from specs or infer from other fields
            let gpu_model = listing.specs.gpu.model.as_deref().unwrap_or("unknown");

            // Normalize GPU type
            let gpu_type = normalize_gpu_type(gpu_model);

            // Get GPU memory
            let gpu_memory_gb = get_gpu_memory(gpu_model);

            // Normalize region to "global"
            let region = normalize_region(listing.location.as_deref().unwrap_or("unknown"));

            // Convert pricing from cents to dollars - skip if total is null/zero
            let hourly_total = listing.price.hourly.total.unwrap_or(0.0);
            if hourly_total == 0.0 {
                continue; // Skip offerings with no pricing
            }
            let hourly_rate = Decimal::from_str(&hourly_total.to_string()).unwrap_or(Decimal::ZERO)
                / Decimal::from(100); // Convert cents to dollars

            // HydraHost supports interruptible pricing (spot-like)
            let spot_rate = listing.interruptible_price.as_ref().and_then(|price| {
                price.hourly.total.map(|total| {
                    Decimal::from_str(&total.to_string()).unwrap_or(Decimal::ZERO)
                        / Decimal::from(100) // Convert cents to dollars
                })
            });

            // Check availability based on status
            // "on demand" means available, other statuses might indicate unavailable
            let availability = listing.status.to_lowercase() == "on demand";

            // Get vcpus - use vcpus if available, otherwise fall back to cores * 2 (typical hyperthreading)
            let vcpu_count = listing
                .specs
                .cpu
                .vcpus
                .or(listing.specs.cpu.thread_count)
                .unwrap_or(listing.specs.cpu.cores * 2);

            // Extract storage information with type details
            let storage = listing.specs.storage.as_ref().and_then(format_storage);

            // Create offering with unique ID using listing ID
            let offering = GpuOffering {
                id: format!("hydrahost-{}", listing.id),
                provider: ProviderEnum::HydraHost,
                gpu_type,
                gpu_memory_gb,
                gpu_count,
                interconnect: None, // HydraHost API doesn't provide interconnect info
                storage,
                deployment_type: None, // Set as NULL for now
                system_memory_gb: listing.specs.memory,
                vcpu_count,
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

#[cfg(test)]
mod tests {

    use crate::providers::hydrahost::types::MarketplaceListing;

    #[test]
    fn test_parse_hydrahost_h100_listing() {
        // Sample H100 listing from HydraHost Brokkr API (8x H100 80GB)
        let json_data = r#"{
            "id": 1200,
            "name": "Supermicro SYS-821GE-TNHR-LC0-HP001-1200",
            "location": "Canada",
            "role": {
                "slug": "Baremetal"
            },
            "status": "on demand",
            "isInterruptibleOnly": false,
            "cluster": {
                "id": null,
                "name": null
            },
            "specs": {
                "cpu": {
                    "model": "Intel(R) Xeon(R) Platinum 8462Y+",
                    "cores": 64,
                    "threadCount": 128,
                    "count": 2
                },
                "gpu": {
                    "model": "NVIDIA H100 80GB HBM3",
                    "count": 8
                },
                "memory": 2048,
                "storage": {
                    "hdd_count": null,
                    "hdd_size": null,
                    "nvme_count": 4,
                    "nvme_size": 30726,
                    "ssd_count": 2,
                    "ssd_size": 480,
                    "total": 31206
                }
            },
            "primary_ip4": "192.251.140.92",
            "primary_ip6": "",
            "networkType": "NAT",
            "vpcCapable": false,
            "price": {
                "stripeId": "price_1RbPXSHxc9tHGXhdhSA9qYKM",
                "monthly": 1488000,
                "weekly": 336000,
                "hourly": {
                    "per_gpu": 250,
                    "per_cpu": 1000,
                    "total": 2000
                }
            },
            "interruptiblePrice": {
                "monthly": 714240,
                "weekly": 161280,
                "hourly": {
                    "per_gpu": 120,
                    "per_cpu": 480,
                    "total": 960
                }
            },
            "activeReservationInvite": null,
            "availableOperatingSystems": [],
            "storageLayouts": null,
            "defaultDiskLayouts": [],
            "supplierPolicyUrl": null
        }"#;

        // Parse the JSON
        let listing: MarketplaceListing =
            serde_json::from_str(json_data).expect("Failed to parse JSON");

        // Verify the parsed data
        assert_eq!(listing.id, 1200);
        assert_eq!(listing.name, "Supermicro SYS-821GE-TNHR-LC0-HP001-1200");
        assert_eq!(listing.location, Some("Canada".to_string()));
        assert_eq!(listing.status, "on demand");

        // Verify GPU specs
        assert_eq!(listing.specs.gpu.count, Some(8));
        assert_eq!(
            listing.specs.gpu.model,
            Some("NVIDIA H100 80GB HBM3".to_string())
        );

        // Verify CPU specs
        assert_eq!(listing.specs.cpu.cores, 64);
        assert_eq!(listing.specs.cpu.thread_count, Some(128));
        assert_eq!(listing.specs.cpu.count, Some(2));

        // Verify memory
        assert_eq!(listing.specs.memory, 2048);

        // Verify storage
        let storage = listing.specs.storage.as_ref().unwrap();
        assert_eq!(storage.nvme_count, Some(4));
        assert_eq!(storage.nvme_size, Some(30726));
        assert_eq!(storage.ssd_count, Some(2));
        assert_eq!(storage.ssd_size, Some(480));
        assert_eq!(storage.total, Some(31206));

        // Verify pricing (in cents)
        assert_eq!(listing.price.hourly.total, Some(2000.0));
        assert_eq!(listing.price.hourly.per_gpu, Some(250.0));

        // Verify interruptible pricing
        let interruptible = listing.interruptible_price.as_ref().unwrap();
        assert_eq!(interruptible.hourly.total, Some(960.0));
        assert_eq!(interruptible.hourly.per_gpu, Some(120.0));

        // Print the parsed data
        println!("Successfully parsed HydraHost H100 listing:");
        println!("  ID: {}", listing.id);
        println!("  Name: {}", listing.name);
        println!(
            "  Location: {}",
            listing.location.as_ref().unwrap_or(&"Unknown".to_string())
        );
        println!("  Status: {}", listing.status);
        println!("  GPU Model: {}", listing.specs.gpu.model.as_ref().unwrap());
        println!("  GPU Count: {}", listing.specs.gpu.count.unwrap());
        println!("  CPU Cores: {}", listing.specs.cpu.cores);
        println!("  CPU Threads: {}", listing.specs.cpu.thread_count.unwrap());
        println!("  System Memory: {}GB", listing.specs.memory);
        println!("  Storage Total: {}GB", storage.total.unwrap_or(0));
        println!(
            "  Hourly Price: ${:.2}",
            listing.price.hourly.total.unwrap() / 100.0
        );
        println!(
            "  Interruptible Price: ${:.2}",
            interruptible.hourly.total.unwrap() / 100.0
        );
    }
}

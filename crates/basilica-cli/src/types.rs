//! CLI-specific type definitions

use basilica_aggregator::GpuOffering;
use basilica_common::types::ComputeCategory;
use basilica_sdk::types::AvailableNode;
use serde::Serialize;

/// Unified GPU representation for CLI display
/// Combines both community cloud (miner) and secure cloud (datacenter) GPUs
#[derive(Debug, Clone, Serialize)]
pub struct UnifiedGpu {
    /// Compute category (secure or community cloud)
    pub compute_category: ComputeCategory,

    /// Provider name (e.g., "Miner", "DataCrunch", "Hyperstack")
    pub provider: String,

    /// GPU type (e.g., "H100", "A100")
    pub gpu_type: String,

    /// Number of GPUs
    pub gpu_count: u32,

    /// Price per hour (formatted string)
    pub price_per_hour: String,

    /// Region/location
    pub region: String,

    /// Availability status
    pub availability: bool,

    /// Original identifier (node_id for community, offering_id for secure)
    pub id: String,
}

impl From<AvailableNode> for UnifiedGpu {
    fn from(avail_node: AvailableNode) -> Self {
        let node = avail_node.node;

        // Extract GPU info from node
        let gpu_type = node
            .gpu_specs
            .first()
            .map(|g| g.name.clone())
            .unwrap_or_else(|| "Unknown".to_string());

        let gpu_count = node.gpu_specs.len() as u32;

        // Community cloud pricing comes from API responses
        let price_per_hour = "varies".to_string();

        let region = node.location.unwrap_or_else(|| "Unknown".to_string());

        UnifiedGpu {
            compute_category: ComputeCategory::CommunityCloud,
            provider: "Miner".to_string(),
            gpu_type,
            gpu_count,
            price_per_hour,
            region,
            availability: true, // If it's in the available list, it's available
            id: node.id,
        }
    }
}

impl From<GpuOffering> for UnifiedGpu {
    fn from(gpu: GpuOffering) -> Self {
        // Calculate total hourly cost (per-GPU rate × gpu_count)
        let total_hourly_cost =
            gpu.hourly_rate_per_gpu * rust_decimal::Decimal::from(gpu.gpu_count);
        UnifiedGpu {
            compute_category: ComputeCategory::SecureCloud,
            provider: gpu.provider.to_string(),
            gpu_type: gpu.gpu_type.to_string(),
            gpu_count: gpu.gpu_count,
            price_per_hour: format!("${}/hr", total_hourly_cost),
            region: gpu.region,
            availability: gpu.availability,
            id: gpu.id,
        }
    }
}

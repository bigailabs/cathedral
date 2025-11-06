use serde::{Deserialize, Serialize};

/// Top-level response from Hyperstack flavors API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct FlavorsResponse {
    pub status: bool,
    pub message: String,
    /// Data is an array of GPU/region groups, each containing flavors
    pub data: Vec<GpuRegionGroup>,
}

/// GPU/region grouping containing multiple flavors
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GpuRegionGroup {
    /// GPU model for this group (e.g., "A100-80G-PCIe", "H100")
    /// Empty string for CPU-only flavors
    pub gpu: String,

    /// Region name (e.g., "CANADA-1", "US-1")
    pub region_name: String,

    /// Flavors available in this GPU/region combination
    pub flavors: Vec<Flavor>,
}

/// Individual flavor/instance type
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Flavor {
    /// Unique flavor ID
    pub id: u32,

    /// Flavor name (e.g., "n3-A100x1", "n3-H100x8")
    pub name: String,

    /// Display name (usually null)
    #[serde(default)]
    pub display_name: Option<String>,

    /// Region name
    pub region_name: String,

    /// Number of CPU cores
    pub cpu: u32,

    /// RAM size in GB (float to handle decimal values)
    pub ram: f64,

    /// Persistent disk size in GB
    pub disk: u32,

    /// Ephemeral storage size in GB (can be null in API, defaults to 0)
    #[serde(default)]
    pub ephemeral: Option<u32>,

    /// GPU model string (e.g., "A100-80G-PCIe", "H100")
    /// Empty string for CPU-only flavors
    pub gpu: String,

    /// Number of GPUs in this flavor
    pub gpu_count: u32,

    /// Whether stock is available
    pub stock_available: bool,

    /// Creation timestamp
    pub created_at: String,

    /// Labels attached to this flavor
    #[serde(default)]
    pub labels: Vec<Label>,

    /// Feature flags
    pub features: Features,
}

/// Label attached to a flavor
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Label {
    pub id: u32,
    pub label: String,
}

/// Feature flags for a flavor
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Features {
    pub network_optimised: bool,
    pub no_hibernation: bool,
    pub no_snapshot: bool,
    pub local_storage_only: bool,
}

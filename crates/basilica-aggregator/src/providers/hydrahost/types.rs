use serde::{Deserialize, Serialize};

/// Response from HydraHost Brokkr API marketplace listings endpoint
pub type ListingsResponse = Vec<MarketplaceListing>;

/// Individual marketplace listing for GPU resources
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct MarketplaceListing {
    /// Unique listing ID
    pub id: u32,

    /// Listing name/title
    pub name: String,

    /// Geographic location (e.g., "Arizona", "Nevada")
    pub location: String,

    /// Listing status (e.g., "on demand", "reserved")
    pub status: String,

    /// Cluster information
    #[serde(default)]
    pub cluster: Option<ClusterInfo>,

    /// Hardware specifications
    pub specs: Specs,

    /// Pricing information
    pub price: Pricing,
}

/// Cluster-level information
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ClusterInfo {
    /// Network bandwidth (e.g., "10Gbps", "100Gbps")
    #[serde(default)]
    pub network: Option<String>,

    /// Number of nodes in cluster
    #[serde(default)]
    pub nodes: Option<u32>,
}

/// Hardware specifications for the listing
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Specs {
    /// CPU specifications
    pub cpu: CpuSpec,

    /// GPU specifications
    pub gpu: GpuSpec,

    /// System memory in GB
    pub memory: u32,
}

/// CPU specifications
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CpuSpec {
    /// Number of physical CPU cores
    pub cores: u32,

    /// Number of vCPUs
    #[serde(rename = "vCpus")]
    pub vcpus: u32,
}

/// GPU specifications
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GpuSpec {
    /// Number of GPUs
    pub count: u32,

    /// GPU model (e.g., "A100", "H100", "4090")
    /// Note: This might need to be inferred from API category parameter
    #[serde(default)]
    pub model: Option<String>,
}

/// Pricing information
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Pricing {
    /// Monthly pricing (may not be relevant for aggregator)
    #[serde(default)]
    pub monthly: Option<f64>,

    /// Hourly pricing breakdown
    pub hourly: HourlyPricing,
}

/// Hourly pricing details
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct HourlyPricing {
    /// Price per CPU core per hour
    #[serde(default)]
    pub per_cpu: Option<f64>,

    /// Price per GPU per hour
    pub per_gpu: f64,

    /// Total hourly price for the entire configuration
    pub total: f64,
}

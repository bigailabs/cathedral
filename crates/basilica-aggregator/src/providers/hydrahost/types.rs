use serde::{Deserialize, Serialize};

/// Response from HydraHost Brokkr API inventory endpoint
pub type ListingsResponse = Vec<MarketplaceListing>;

/// Individual inventory listing for GPU resources
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct MarketplaceListing {
    /// Unique listing ID
    pub id: u32,

    /// Listing name/title
    pub name: String,

    /// Geographic location (e.g., "Arizona", "Nevada")
    #[serde(default)]
    pub location: Option<String>,

    /// Device role
    #[serde(default)]
    pub role: Option<Role>,

    /// Listing status (e.g., "on demand", "reserve", "preorder")
    pub status: String,

    /// Primary IPv4 address
    #[serde(default)]
    pub primary_ip4: Option<String>,

    /// Primary IPv6 address
    #[serde(default)]
    pub primary_ip6: Option<String>,

    /// Cluster information
    #[serde(default)]
    pub cluster: Option<ClusterInfo>,

    /// Hardware specifications
    pub specs: Specs,

    /// Standard pricing information
    pub price: Pricing,

    /// Available operating systems
    #[serde(rename = "availableOperatingSystems", default)]
    pub available_operating_systems: Vec<OperatingSystem>,

    /// Storage layout options
    #[serde(rename = "storageLayouts", default)]
    pub storage_layouts: Option<serde_json::Value>,

    /// Default disk layouts
    #[serde(rename = "defaultDiskLayouts", default)]
    pub default_disk_layouts: Vec<serde_json::Value>,

    /// Network type (NAT or Public)
    #[serde(rename = "networkType", default)]
    pub network_type: Option<String>,

    /// Whether VPC capable
    #[serde(rename = "vpcCapable", default)]
    pub vpc_capable: bool,

    /// Active reservation invite
    #[serde(rename = "activeReservationInvite", default)]
    pub active_reservation_invite: Option<serde_json::Value>,

    /// Supplier policy URL
    #[serde(rename = "supplierPolicyUrl", default)]
    pub supplier_policy_url: Option<String>,
}

/// Device role information
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Role {
    /// Role slug
    pub slug: String,
}

/// Cluster-level information
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ClusterInfo {
    /// Cluster ID
    #[serde(default)]
    pub id: Option<u32>,

    /// Cluster name
    #[serde(default)]
    pub name: Option<String>,
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

    /// Storage specifications
    #[serde(default)]
    pub storage: Option<StorageSpec>,
}

/// CPU specifications
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CpuSpec {
    /// CPU model name
    #[serde(default)]
    pub model: Option<String>,

    /// Number of physical CPU cores
    pub cores: u32,

    /// Number of CPU sockets/count
    #[serde(default)]
    pub count: Option<u32>,

    /// Number of vCPUs
    #[serde(rename = "vCpus", default)]
    pub vcpus: Option<u32>,

    /// Thread count
    #[serde(rename = "threadCount", default)]
    pub thread_count: Option<u32>,
}

/// GPU specifications
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GpuSpec {
    /// Number of GPUs (can be null for CPU-only machines)
    #[serde(default)]
    pub count: Option<u32>,

    /// GPU model (e.g., "A100", "H100", "4090")
    #[serde(default)]
    pub model: Option<String>,
}

/// Storage specifications
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct StorageSpec {
    /// HDD count
    #[serde(default)]
    pub hdd_count: Option<u32>,

    /// HDD size in GB
    #[serde(default)]
    pub hdd_size: Option<u32>,

    /// SSD count
    #[serde(default)]
    pub ssd_count: Option<u32>,

    /// SSD size in GB
    #[serde(default)]
    pub ssd_size: Option<u32>,

    /// NVMe count
    #[serde(default)]
    pub nvme_count: Option<u32>,

    /// NVMe size in GB
    #[serde(default)]
    pub nvme_size: Option<u32>,

    /// Total storage
    #[serde(default)]
    pub total: Option<u32>,
}

/// Operating system information
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct OperatingSystem {
    /// OS ID
    #[serde(default)]
    pub id: Option<String>,

    /// OS name
    pub name: String,

    /// OS slug
    pub slug: String,

    /// Netbox platform ID
    #[serde(rename = "netboxPlatformId", default)]
    pub netbox_platform_id: Option<u32>,

    /// OS description
    #[serde(default)]
    pub description: Option<String>,

    /// OS distribution
    #[serde(rename = "osDistribution", default)]
    pub os_distribution: Option<String>,

    /// OS version
    #[serde(rename = "osVersion", default)]
    pub os_version: Option<String>,
}

/// Pricing information
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Pricing {
    /// Stripe price ID
    #[serde(rename = "stripeId", default)]
    pub stripe_id: Option<String>,

    /// Monthly pricing (in cents)
    #[serde(default)]
    pub monthly: Option<u64>,

    /// Weekly pricing (in cents)
    #[serde(default)]
    pub weekly: Option<u64>,

    /// Hourly pricing breakdown
    pub hourly: HourlyPricing,
}

/// Hourly pricing details
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct HourlyPricing {
    /// Price per CPU core per hour (in cents)
    #[serde(default)]
    pub per_cpu: Option<f64>,

    /// Price per GPU per hour (in cents)
    #[serde(default)]
    pub per_gpu: Option<f64>,

    /// Total hourly price for the entire configuration (in cents)
    #[serde(default)]
    pub total: Option<f64>,
}

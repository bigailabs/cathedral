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

// ============================================================================
// SSH Key Management Types
// ============================================================================

/// SSH keypair response from Hyperstack API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Keypair {
    pub id: u32,
    pub name: String,
    pub public_key: String,
    pub fingerprint: String,
    pub created_at: String,
}

/// Request to create a new SSH keypair
#[derive(Debug, Clone, Serialize)]
pub struct CreateKeypairRequest {
    pub name: String,
    pub environment_name: String,
    pub public_key: String,
}

/// Response from creating a keypair
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CreateKeypairResponse {
    pub status: bool,
    pub message: String,
    pub keypair: Keypair,
}

// ============================================================================
// OS Image Types
// ============================================================================

/// OS image response from Hyperstack API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Image {
    pub id: u32,
    pub name: String,
    pub description: Option<String>,
    pub version: Option<String>,
    pub region_name: String,
    pub size: Option<u32>,
    pub created_at: String,
}

/// Response for listing images
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ImagesResponse {
    pub status: bool,
    pub message: String,
    pub images: Vec<Image>,
}

// ============================================================================
// Environment Types
// ============================================================================

/// Environment response from Hyperstack API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Environment {
    pub id: u32,
    pub name: String,
    pub region: String,
    pub created_at: String,
}

/// Response for listing environments
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct EnvironmentsResponse {
    pub status: bool,
    pub message: String,
    pub environments: Vec<Environment>,
}

// ============================================================================
// Virtual Machine Deployment Types
// ============================================================================

/// Request to deploy a new virtual machine
#[derive(Debug, Clone, Serialize)]
pub struct DeployVmRequest {
    pub name: String,
    pub environment_name: String,
    pub image_name: String,
    pub flavor_name: String,
    pub key_name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub user_data: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub assign_floating_ip: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub count: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub create_bootable_volume: Option<bool>,
}

/// Virtual machine status from Hyperstack API
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum VmStatus {
    Active,
    Building,
    Error,
    HardReboot,
    Migrating,
    Password,
    Paused,
    Reboot,
    Rebuild,
    Rescued,
    Resized,
    RevertResize,
    ShelvedOffloaded,
    ShutOff,
    #[serde(rename = "SOFT_DELETED")]
    SoftDeleted,
    Suspended,
    Unknown,
    #[serde(rename = "VERIFY_RESIZE")]
    VerifyResize,
}

/// Virtual machine details from Hyperstack API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct VirtualMachine {
    pub id: u32,
    pub name: String,
    pub status: String, // Using String instead of enum for flexibility
    pub environment_name: String,
    pub flavor_name: String,
    pub image_name: Option<String>,
    pub key_name: Option<String>,
    #[serde(default)]
    pub fixed_ip: Option<String>,
    #[serde(default)]
    pub floating_ip: Option<String>,
    #[serde(default)]
    pub floating_ip_status: Option<String>,
    pub created_at: String,
    #[serde(default)]
    pub security_rules: Vec<SecurityRule>,
}

/// Security rule for a virtual machine
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct SecurityRule {
    pub id: u32,
    pub direction: String,
    pub ethertype: String,
    pub protocol: String,
    pub port_range_min: Option<u32>,
    pub port_range_max: Option<u32>,
    pub remote_ip_prefix: String,
    pub created_at: String,
}

/// Response from deploying a VM
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DeployVmResponse {
    pub status: bool,
    pub message: String,
    pub virtual_machines: Vec<VirtualMachine>,
}

/// Response for getting VM details
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GetVmResponse {
    pub status: bool,
    pub message: String,
    pub virtual_machine: VirtualMachine,
}

/// Request to delete a virtual machine
#[derive(Debug, Clone, Serialize)]
pub struct DeleteVmRequest {
    pub virtual_machine_ids: Vec<u32>,
}

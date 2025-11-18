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
// Pricebook Types
// ============================================================================

/// Response from Hyperstack pricebook API
/// Returns array of resource pricing items
pub type PricebookResponse = Vec<PricebookItem>;

/// Individual pricebook item containing resource pricing
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PricebookItem {
    /// Unique pricebook ID
    pub id: u32,

    /// Resource name (e.g., "H100-80G-PCIe", "A100-80G-SXM4", "vCPU", "RAM")
    pub name: String,

    /// Actual hourly rate (after discounts if applicable)
    /// Returned as string by API (e.g., "1.350000000")
    pub value: String,

    /// Original hourly rate before discounts
    /// Returned as string by API
    pub original_value: String,

    /// Whether a discount has been applied to this resource
    pub discount_applied: bool,

    /// Optional start time for time-based pricing
    #[serde(default)]
    pub start_time: Option<String>,

    /// Optional end time for time-based pricing
    #[serde(default)]
    pub end_time: Option<String>,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub security_rules: Option<Vec<SecurityRuleRequest>>,
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

/// Security rule for a virtual machine request
#[derive(Debug, Clone, Serialize)]
pub struct SecurityRuleRequest {
    pub direction: String,
    pub ethertype: String,
    pub protocol: String,
    pub port_range_min: u32,
    pub port_range_max: u32,
    pub remote_ip_prefix: String,
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

/// Nested keypair info in deploy response
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NestedKeypair {
    pub name: String,
}

/// Nested environment info in deploy response
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NestedEnvironment {
    pub name: String,
}

/// Nested image info in deploy response
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NestedImage {
    pub name: String,
}

/// Nested flavor info in deploy response
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NestedFlavor {
    pub name: String,
}

/// Instance structure from deploy VM response
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DeployVmInstance {
    pub id: u32,
    pub name: String,
    pub status: String,
    pub created_at: String,
    pub keypair: NestedKeypair,
    pub environment: NestedEnvironment,
    pub image: NestedImage,
    pub flavor: NestedFlavor,
    #[serde(default)]
    pub fixed_ip: Option<String>,
    #[serde(default)]
    pub floating_ip: Option<String>,
    #[serde(default)]
    pub floating_ip_status: Option<String>,
}

impl From<DeployVmInstance> for VirtualMachine {
    fn from(instance: DeployVmInstance) -> Self {
        VirtualMachine {
            id: instance.id,
            name: instance.name,
            status: instance.status,
            environment_name: instance.environment.name,
            flavor_name: instance.flavor.name,
            image_name: Some(instance.image.name),
            key_name: Some(instance.keypair.name),
            fixed_ip: instance.fixed_ip,
            floating_ip: instance.floating_ip,
            floating_ip_status: instance.floating_ip_status,
            created_at: instance.created_at,
            security_rules: vec![], // Deploy response doesn't include security rules
        }
    }
}

/// Response from deploying a VM
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DeployVmResponse {
    pub status: bool,
    pub message: String,
    pub instances: Vec<DeployVmInstance>,
}

/// Response for getting VM details
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GetVmResponse {
    pub status: bool,
    pub message: String,
    pub instance: DeployVmInstance,
}

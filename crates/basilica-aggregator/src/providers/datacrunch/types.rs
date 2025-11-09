use serde::{Deserialize, Serialize};

/// Instance type response from DataCrunch API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct InstanceType {
    pub id: String,
    pub instance_type: String,
    pub price_per_hour: String, // DataCrunch returns prices as strings
    pub description: String,
    pub cpu: CpuSpec,
    pub gpu: GpuSpec,
    pub memory: MemorySpec,
    pub gpu_memory: GpuMemorySpec,
    pub storage: StorageSpec,
    #[serde(default)]
    pub model: Option<String>, // GPU model like "B300", "H100"
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CpuSpec {
    pub number_of_cores: u32, // Field name is number_of_cores, not cores
    pub description: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GpuSpec {
    pub number_of_gpus: u32, // Field name is number_of_gpus, not count
    pub description: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct MemorySpec {
    pub size_in_gigabytes: u32, // Field name is size_in_gigabytes, not size_gb
    pub description: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GpuMemorySpec {
    pub size_in_gigabytes: u32, // Field name is size_in_gigabytes, not size_gb
    pub description: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct StorageSpec {
    pub description: String, // Storage only has description, no size_gb
}

/// Location response from DataCrunch API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Location {
    pub code: String,
    pub name: String,
    pub country_code: String,
}

/// Instance availability response - per location
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct LocationAvailability {
    pub location_code: String,
    pub availabilities: Vec<String>, // List of available instance type IDs
}

// ============================================================================
// SSH Key Management Types
// ============================================================================

/// SSH key response from DataCrunch API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct SshKey {
    pub id: String,
    pub name: String,
    #[serde(rename = "key")]
    pub public_key: String,
}

/// Request to create a new SSH key
#[derive(Debug, Clone, Serialize)]
pub struct CreateSshKeyRequest {
    pub name: String,
    pub key: String, // Public key content
}

// ============================================================================
// OS Image Types
// ============================================================================

/// OS image response from DataCrunch API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct OsImage {
    pub image_type: String, // e.g., "ubuntu-22.04-cuda-12.4-docker"
    pub description: String,
}

// ============================================================================
// Instance Deployment Types
// ============================================================================

/// Request to deploy a new instance
#[derive(Debug, Clone, Serialize)]
pub struct DeployInstanceRequest {
    pub instance_type: String,
    pub image: String,
    pub hostname: String,
    pub description: String,
    pub ssh_key_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub location_code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub contract: Option<String>, // "PAY_AS_YOU_GO", "LONG_TERM", "SPOT"
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pricing: Option<String>, // "FIXED_PRICE", "DYNAMIC_PRICE"
}

/// Instance status values from DataCrunch API
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum InstanceStatus {
    Running,
    Provisioning,
    Offline,
    Ordered,
    Error,
    Deleting,
    Validating,
    Discontinued,
    #[serde(rename = "no_capacity")]
    NoCapacity,
    Unknown,
    #[serde(rename = "notfound")]
    NotFound,
    New,
}

/// Full instance details from DataCrunch API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Instance {
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ip: Option<String>,
    pub hostname: String,
    pub instance_type: String,
    pub status: InstanceStatus,
    pub created_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub image: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ssh_key_ids: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub os_volume_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub jupyter_token: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub startup_script_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub price_per_hour: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub contract: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pricing: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    // Include raw response for any additional fields
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

/// Request to perform instance action (delete, shutdown, etc.)
#[derive(Debug, Clone, Serialize)]
pub struct InstanceActionRequest {
    pub action: String, // "delete", "boot", "start", "shutdown", etc.
    pub instance_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub volume_ids: Option<Vec<String>>, // For delete action
}

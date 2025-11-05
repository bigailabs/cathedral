use serde::{Deserialize, Serialize};

/// Instance type response from DataCrunch API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct InstanceType {
    pub id: String,
    pub instance_type: String,
    pub price_per_hour: String, // DataCrunch returns prices as strings
    #[serde(default)]
    pub spot_price: Option<String>, // Field name is spot_price, not spot_price_per_hour
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

/// Instance availability response
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct InstanceAvailability {
    pub instance_type: String,
    pub location_code: String,
    pub available: bool,
    #[serde(default)]
    pub is_spot: bool,
}

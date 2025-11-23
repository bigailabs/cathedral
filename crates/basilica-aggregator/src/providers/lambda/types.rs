use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Top-level response from Lambda instance-types API
/// Maps instance type names (e.g., "gpu_1x_a10") to their details
pub type InstanceTypesResponse = HashMap<String, InstanceTypeWrapper>;

/// Wrapper containing instance type details and regional availability
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct InstanceTypeWrapper {
    pub instance_type: InstanceType,
    #[serde(default)]
    pub regions_with_capacity_available: Vec<Region>,
}

/// Instance type details from Lambda API
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct InstanceType {
    pub name: String,
    pub price_cents_per_hour: u64, // Lambda returns prices in cents
    pub description: String,       // e.g., "1x A10 (24 GB PCIe)"
    pub specs: Specs,
}

/// Hardware specifications for the instance
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Specs {
    pub vcpus: u32,
    pub memory_gib: u32,
    pub storage_gib: u32,
}

/// Region availability information
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Region {
    pub name: String,        // e.g., "us-west-1"
    pub description: String, // e.g., "California, USA"
}

use basilica_common::types::GpuCategory;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// Provider identifier
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "TEXT")]
#[serde(rename_all = "lowercase")]
pub enum Provider {
    DataCrunch,
    Hyperstack,
    Lambda,
    HydraHost,
}

impl Provider {
    pub fn as_str(&self) -> &'static str {
        match self {
            Provider::DataCrunch => "datacrunch",
            Provider::Hyperstack => "hyperstack",
            Provider::Lambda => "lambda",
            Provider::HydraHost => "hydrahost",
        }
    }
}

impl std::fmt::Display for Provider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

impl std::str::FromStr for Provider {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "datacrunch" => Ok(Provider::DataCrunch),
            "hyperstack" => Ok(Provider::Hyperstack),
            "lambda" => Ok(Provider::Lambda),
            "hydrahost" => Ok(Provider::HydraHost),
            _ => Err(format!("Unknown provider: {}", s)),
        }
    }
}

/// Unified GPU offering structure
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GpuOffering {
    pub id: String,
    pub provider: Provider,
    pub gpu_type: GpuCategory,
    pub gpu_memory_gb: u32, // GPU memory per card
    pub gpu_count: u32,
    pub system_memory_gb: u32, // System RAM
    pub vcpu_count: u32,
    pub region: String,
    #[serde(with = "rust_decimal::serde::str")]
    pub hourly_rate: Decimal,
    #[serde(
        with = "rust_decimal::serde::str_option",
        skip_serializing_if = "Option::is_none"
    )]
    pub spot_rate: Option<Decimal>,
    pub availability: bool,
    pub fetched_at: DateTime<Utc>,
    #[serde(skip_serializing)] // Never expose in API
    pub raw_metadata: serde_json::Value,
}

/// Provider health status
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderHealth {
    pub provider: Provider,
    pub is_healthy: bool,
    pub last_success_at: Option<DateTime<Utc>>,
    pub last_error: Option<String>,
}

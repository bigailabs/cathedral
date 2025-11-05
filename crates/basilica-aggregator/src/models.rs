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
}

impl Provider {
    pub fn as_str(&self) -> &'static str {
        match self {
            Provider::DataCrunch => "datacrunch",
            Provider::Hyperstack => "hyperstack",
            Provider::Lambda => "lambda",
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
            _ => Err(format!("Unknown provider: {}", s)),
        }
    }
}

/// Canonical GPU types
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "TEXT")]
pub enum GpuType {
    #[serde(rename = "H100_80GB")]
    H100_80GB,
    #[serde(rename = "H100_94GB")]
    H100_94GB,
    #[serde(rename = "A100_40GB")]
    A100_40GB,
    #[serde(rename = "A100_80GB")]
    A100_80GB,
    #[serde(rename = "V100_16GB")]
    V100_16GB,
    #[serde(rename = "V100_32GB")]
    V100_32GB,
    #[serde(rename = "A10_24GB")]
    A10_24GB,
    #[serde(rename = "A6000_48GB")]
    A6000_48GB,
    #[serde(rename = "L40_48GB")]
    L40_48GB,
    #[serde(rename = "B200")]
    B200,
    #[serde(rename = "GH200")]
    GH200,
}

impl GpuType {
    pub fn as_str(&self) -> &'static str {
        match self {
            GpuType::H100_80GB => "H100_80GB",
            GpuType::H100_94GB => "H100_94GB",
            GpuType::A100_40GB => "A100_40GB",
            GpuType::A100_80GB => "A100_80GB",
            GpuType::V100_16GB => "V100_16GB",
            GpuType::V100_32GB => "V100_32GB",
            GpuType::A10_24GB => "A10_24GB",
            GpuType::A6000_48GB => "A6000_48GB",
            GpuType::L40_48GB => "L40_48GB",
            GpuType::B200 => "B200",
            GpuType::GH200 => "GH200",
        }
    }
}

impl std::fmt::Display for GpuType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

impl std::str::FromStr for GpuType {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "H100_80GB" => Ok(GpuType::H100_80GB),
            "H100_94GB" => Ok(GpuType::H100_94GB),
            "A100_40GB" => Ok(GpuType::A100_40GB),
            "A100_80GB" => Ok(GpuType::A100_80GB),
            "V100_16GB" => Ok(GpuType::V100_16GB),
            "V100_32GB" => Ok(GpuType::V100_32GB),
            "A10_24GB" => Ok(GpuType::A10_24GB),
            "A6000_48GB" => Ok(GpuType::A6000_48GB),
            "L40_48GB" => Ok(GpuType::L40_48GB),
            "B200" => Ok(GpuType::B200),
            "GH200" => Ok(GpuType::GH200),
            _ => Err(format!("Unknown GPU type: {}", s)),
        }
    }
}

/// Unified GPU offering structure
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GpuOffering {
    pub id: String,
    pub provider: Provider,
    pub gpu_type: GpuType,
    pub gpu_count: u32,
    pub memory_gb: u32,
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

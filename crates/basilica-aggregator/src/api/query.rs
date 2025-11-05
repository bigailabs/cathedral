use crate::models::{GpuType, Provider};
use rust_decimal::Decimal;
use serde::Deserialize;
use std::str::FromStr;

#[derive(Debug, Deserialize)]
pub struct GpuPriceQuery {
    pub gpu_type: Option<String>,
    pub region: Option<String>,
    pub provider: Option<String>,
    pub min_price: Option<String>,
    pub max_price: Option<String>,
    pub available_only: Option<bool>,
    pub sort_by: Option<String>,
}

impl GpuPriceQuery {
    pub fn gpu_type(&self) -> Option<GpuType> {
        self.gpu_type
            .as_ref()
            .and_then(|s| GpuType::from_str(s).ok())
    }

    pub fn provider(&self) -> Option<Provider> {
        self.provider
            .as_ref()
            .and_then(|s| Provider::from_str(s).ok())
    }

    pub fn min_price(&self) -> Option<Decimal> {
        self.min_price
            .as_ref()
            .and_then(|s| Decimal::from_str(s).ok())
    }

    pub fn max_price(&self) -> Option<Decimal> {
        self.max_price
            .as_ref()
            .and_then(|s| Decimal::from_str(s).ok())
    }
}

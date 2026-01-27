use anyhow::Result;
use chrono::Duration;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CollateralConfig {
    #[serde(default = "default_collateral_enabled")]
    pub enabled: bool,
    #[serde(default = "default_shadow_mode")]
    pub shadow_mode: bool,
    #[serde(default = "default_taostats_base_url")]
    pub taostats_base_url: String,
    #[serde(default = "default_alpha_price_path")]
    pub alpha_price_path: String,
    #[serde(default = "default_price_refresh_interval_secs")]
    pub price_refresh_interval_secs: u64,
    #[serde(default = "default_price_stale_after_secs")]
    pub price_stale_after_secs: u64,
    #[serde(default = "default_warning_threshold_multiplier")]
    pub warning_threshold_multiplier: f64,
    #[serde(default = "default_grace_period_hours")]
    pub grace_period_hours: u64,
    #[serde(default = "default_minimum_usd_per_gpu")]
    pub minimum_usd_per_gpu: HashMap<String, f64>,
    #[serde(default)]
    pub contract_address: Option<String>,
    #[serde(default = "default_collateral_network")]
    pub network: String,
    #[serde(default = "default_slash_fraction")]
    pub slash_fraction: f64,
    #[serde(default)]
    pub trustee_private_key_file: Option<PathBuf>,
    #[serde(default = "default_evidence_base_url")]
    pub evidence_base_url: String,
    #[serde(default = "default_evidence_storage_path")]
    pub evidence_storage_path: PathBuf,
}

impl Default for CollateralConfig {
    fn default() -> Self {
        Self {
            enabled: default_collateral_enabled(),
            shadow_mode: default_shadow_mode(),
            taostats_base_url: default_taostats_base_url(),
            alpha_price_path: default_alpha_price_path(),
            price_refresh_interval_secs: default_price_refresh_interval_secs(),
            price_stale_after_secs: default_price_stale_after_secs(),
            warning_threshold_multiplier: default_warning_threshold_multiplier(),
            grace_period_hours: default_grace_period_hours(),
            minimum_usd_per_gpu: default_minimum_usd_per_gpu(),
            contract_address: None,
            network: default_collateral_network(),
            slash_fraction: default_slash_fraction(),
            trustee_private_key_file: None,
            evidence_base_url: default_evidence_base_url(),
            evidence_storage_path: default_evidence_storage_path(),
        }
    }
}

impl CollateralConfig {
    pub fn price_refresh_interval(&self) -> Duration {
        Duration::seconds(self.price_refresh_interval_secs as i64)
    }

    pub fn price_stale_after(&self) -> Duration {
        Duration::seconds(self.price_stale_after_secs as i64)
    }

    pub fn grace_period(&self) -> Duration {
        Duration::hours(self.grace_period_hours as i64)
    }

    pub fn validate(&self) -> Result<()> {
        if !self.enabled {
            return Ok(());
        }
        if self.taostats_base_url.trim().is_empty() {
            anyhow::bail!("collateral.taostats_base_url cannot be empty");
        }
        if self.alpha_price_path.trim().is_empty() {
            anyhow::bail!("collateral.alpha_price_path cannot be empty");
        }
        if self.price_refresh_interval_secs == 0 {
            anyhow::bail!("collateral.price_refresh_interval_secs must be > 0");
        }
        if self.price_stale_after_secs == 0 {
            anyhow::bail!("collateral.price_stale_after_secs must be > 0");
        }
        if self.warning_threshold_multiplier < 1.0 {
            anyhow::bail!("collateral.warning_threshold_multiplier must be >= 1.0");
        }
        if self.grace_period_hours == 0 {
            anyhow::bail!("collateral.grace_period_hours must be > 0");
        }
        if self.minimum_usd_per_gpu.is_empty() {
            anyhow::bail!("collateral.minimum_usd_per_gpu cannot be empty");
        }
        if !(0.0 < self.slash_fraction && self.slash_fraction <= 1.0) {
            anyhow::bail!("collateral.slash_fraction must be within (0.0, 1.0]");
        }
        Ok(())
    }
}

fn default_collateral_enabled() -> bool {
    false
}

fn default_shadow_mode() -> bool {
    true
}

fn default_taostats_base_url() -> String {
    "https://api.taostats.io".to_string()
}

fn default_alpha_price_path() -> String {
    "/alpha/price".to_string()
}

fn default_price_refresh_interval_secs() -> u64 {
    900
}

fn default_price_stale_after_secs() -> u64 {
    3600
}

fn default_warning_threshold_multiplier() -> f64 {
    1.5
}

fn default_grace_period_hours() -> u64 {
    24
}

fn default_minimum_usd_per_gpu() -> HashMap<String, f64> {
    let mut map = HashMap::new();
    map.insert("H100".to_string(), 50.0);
    map.insert("A100".to_string(), 25.0);
    map.insert("B200".to_string(), 75.0);
    map.insert("DEFAULT".to_string(), 10.0);
    map
}

fn default_collateral_network() -> String {
    "mainnet".to_string()
}

fn default_slash_fraction() -> f64 {
    1.0
}

fn default_evidence_base_url() -> String {
    "https://validator.example.com/evidence".to_string()
}

fn default_evidence_storage_path() -> PathBuf {
    PathBuf::from("./evidence")
}

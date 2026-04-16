use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;

/// Default burn UID for Cathedral validator emissions
pub const DEFAULT_BURN_UID: u16 = 204;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EmissionConfig {
    /// Forced minimum burn percentage (0.0-100.0). Reduces effective emission capacity
    /// before dynamic burn calculation. None = no forced burn.
    #[serde(default)]
    pub forced_burn_percentage: Option<f64>,

    /// UID to send burn weights to
    pub burn_uid: u16,

    /// Blocks between weight setting
    pub weight_set_interval_blocks: u64,

    /// Version key for weight setting operations
    /// This prevents replay attacks by incrementing with each weight set
    pub weight_version_key: u64,
}

impl EmissionConfig {
    /// Validate the emission configuration
    pub fn validate(&self) -> Result<()> {
        // Validate forced burn percentage if set
        if let Some(pct) = self.forced_burn_percentage {
            if !pct.is_finite() || !(0.0..100.0).contains(&pct) {
                return Err(anyhow!(
                    "Forced burn percentage must be >= 0.0 and < 100.0, got: {}",
                    pct
                ));
            }
        }

        // Validate weight set interval
        if self.weight_set_interval_blocks == 0 {
            return Err(anyhow!(
                "Weight set interval blocks must be greater than 0, got: {}",
                self.weight_set_interval_blocks
            ));
        }

        Ok(())
    }

    /// Load configuration from a TOML file
    pub fn from_toml_file(path: &Path) -> Result<Self> {
        let content = std::fs::read_to_string(path)
            .map_err(|e| anyhow!("Failed to read config file {}: {}", path.display(), e))?;

        let config: Self =
            toml::from_str(&content).map_err(|e| anyhow!("Failed to parse TOML config: {}", e))?;

        config.validate()?;
        Ok(config)
    }

    /// Merge this config with defaults for missing fields
    pub fn merge_with_defaults(mut self) -> Self {
        let default_config = Self::default();

        // Ensure other fields have reasonable defaults if they're zero/invalid
        if self.weight_set_interval_blocks == 0 {
            self.weight_set_interval_blocks = default_config.weight_set_interval_blocks;
        }

        self
    }

    /// Create a configuration for testing with custom values
    pub fn for_testing() -> Self {
        Self {
            forced_burn_percentage: Some(10.0),
            burn_uid: 999,
            weight_set_interval_blocks: 360,
            weight_version_key: 0,
        }
    }
}

impl Default for EmissionConfig {
    fn default() -> Self {
        Self {
            forced_burn_percentage: None,
            burn_uid: DEFAULT_BURN_UID,
            weight_set_interval_blocks: 360,
            weight_version_key: 0,
        }
    }
}

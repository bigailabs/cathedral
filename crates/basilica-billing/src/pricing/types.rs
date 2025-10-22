use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Configuration for dynamic pricing system
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PricingConfig {
    /// Enable dynamic pricing from external sources
    pub enabled: bool,

    /// Global discount percentage (negative = discount, positive = markup)
    /// Example: -20.0 means 20% discount from market price
    pub global_discount_percent: Decimal,

    /// Per-GPU model discount overrides
    /// Example: {"H100": -15.0, "A100": -25.0}
    pub gpu_discounts: HashMap<String, Decimal>,

    /// Price update interval in seconds (default: 86400 = daily)
    /// Sync runs every N seconds from service start
    pub update_interval_seconds: u64,

    /// Cache TTL in seconds (default: 86400 = 24 hours)
    pub cache_ttl_seconds: u64,

    /// Fallback to static prices if external fetch fails
    pub fallback_to_static: bool,

    /// Price sources to query
    pub sources: Vec<PriceSource>,

    /// Price aggregation strategy
    pub aggregation_strategy: PriceAggregationStrategy,

    /// Marketplace API key (required when sources contains Marketplace)
    /// Should be loaded from environment variable or AWS Secrets Manager
    pub marketplace_api_key: Option<String>,

    /// Marketplace API URL (optional, defaults to production)
    #[serde(default = "default_marketplace_url")]
    pub marketplace_api_url: String,

    /// Only return available instances from marketplace
    #[serde(default = "default_true")]
    pub marketplace_available_only: bool,
}

fn default_marketplace_url() -> String {
    "https://api.shadeform.ai/v1".to_string()
}

fn default_true() -> bool {
    true
}

fn default_num_gpus() -> u32 {
    1
}

impl Default for PricingConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            global_discount_percent: Decimal::from(-50), // 50% discount
            gpu_discounts: HashMap::new(),
            update_interval_seconds: 86400, // 24 hours (daily)
            cache_ttl_seconds: 86400,       // 24 hours
            fallback_to_static: true,
            sources: vec![PriceSource::Marketplace],
            aggregation_strategy: PriceAggregationStrategy::Average,
            marketplace_api_key: None,
            marketplace_api_url: default_marketplace_url(),
            marketplace_available_only: true,
        }
    }
}

/// External price sources
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum PriceSource {
    /// GPU marketplace aggregator (aggregates VastAI, RunPod, Lambda Labs, TensorDock, and more)
    Marketplace,
    /// Custom API endpoint
    Custom { url: String },
}

/// Strategy for aggregating prices from multiple sources
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum PriceAggregationStrategy {
    /// Use minimum price across all sources
    Minimum,
    /// Use average price across all sources
    Average,
}

/// GPU price information from external sources
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GpuPrice {
    /// GPU model name (e.g., "H100", "A100")
    pub gpu_model: String,

    /// VRAM in GB
    pub vram_gb: Option<u32>,

    /// Number of GPUs in this configuration (e.g., 1, 2, 4, 8)
    /// Used to normalize prices for fair comparison across configurations
    #[serde(default = "default_num_gpus")]
    pub num_gpus: u32,

    /// Raw market price per hour (before discount)
    pub market_price_per_hour: Decimal,

    /// Discounted price per hour (after applying discount)
    pub discounted_price_per_hour: Decimal,

    /// Discount applied as percentage
    pub discount_percent: Decimal,

    /// Source of the price (e.g., "aws", "aggregated")
    pub source: String,

    /// Provider (e.g., "aws", "azure")
    pub provider: String,

    /// Location/region
    pub location: Option<String>,

    /// Instance type name
    pub instance_name: Option<String>,

    /// Last updated timestamp
    pub updated_at: DateTime<Utc>,

    /// Whether this is a spot/interruptible instance
    pub is_spot: bool,
}

impl GpuPrice {
    /// Apply discount percentage to market price
    pub fn apply_discount(&mut self, discount_percent: Decimal) {
        self.discount_percent = discount_percent;
        // discount_percent is negative for discounts, positive for markup
        // Formula: discounted = market * (1 + discount%/100)
        // Example: -20% discount: 100 * (1 + (-20/100)) = 100 * 0.8 = 80
        // Example: +10% markup: 100 * (1 + (10/100)) = 100 * 1.1 = 110
        let discount_multiplier = Decimal::ONE + (discount_percent / Decimal::from(100));
        self.discounted_price_per_hour = self.market_price_per_hour * discount_multiplier;
    }

    /// Get the effective discount for a GPU model (considers both global and per-GPU)
    pub fn effective_discount(
        global_discount: Decimal,
        gpu_discounts: &HashMap<String, Decimal>,
        gpu_model: &str,
    ) -> Decimal {
        gpu_discounts
            .get(gpu_model)
            .copied()
            .unwrap_or(global_discount)
    }
}

/// Aggregated GPU price (computed from multiple providers)
/// This type represents a synthetic price that has been aggregated across multiple
/// provider prices. It does not have provider-specific fields like provider name,
/// location, or instance name.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AggregatedGpuPrice {
    /// GPU model name (e.g., "H100", "A100")
    pub gpu_model: String,

    /// VRAM in GB
    pub vram_gb: Option<u32>,

    /// Number of GPUs (normalized to 1 after aggregation)
    pub num_gpus: u32,

    /// Raw market price per hour (before discount)
    pub market_price_per_hour: Decimal,

    /// Discounted price per hour (after applying discount)
    pub discounted_price_per_hour: Decimal,

    /// Discount applied as percentage
    pub discount_percent: Decimal,

    /// Aggregation strategy used to compute this price
    pub aggregation_strategy: PriceAggregationStrategy,

    /// Last updated timestamp
    pub updated_at: DateTime<Utc>,

    /// Whether this represents spot/interruptible instances
    pub is_spot: bool,
}

impl AggregatedGpuPrice {
    /// Apply discount percentage to market price
    pub fn apply_discount(&mut self, discount_percent: Decimal) {
        self.discount_percent = discount_percent;
        let discount_multiplier = Decimal::ONE + (discount_percent / Decimal::from(100));
        self.discounted_price_per_hour = self.market_price_per_hour * discount_multiplier;
    }
}

/// Convert aggregated price to regular GpuPrice for storage in cache
impl From<AggregatedGpuPrice> for GpuPrice {
    fn from(aggregated: AggregatedGpuPrice) -> Self {
        GpuPrice {
            gpu_model: aggregated.gpu_model,
            vram_gb: aggregated.vram_gb,
            num_gpus: aggregated.num_gpus,
            market_price_per_hour: aggregated.market_price_per_hour,
            discounted_price_per_hour: aggregated.discounted_price_per_hour,
            discount_percent: aggregated.discount_percent,
            source: format!("aggregated_{:?}", aggregated.aggregation_strategy).to_lowercase(),
            provider: "aggregated".to_string(),
            location: None,
            instance_name: None,
            updated_at: aggregated.updated_at,
            is_spot: aggregated.is_spot,
        }
    }
}

/// Filter for querying prices
#[derive(Debug, Clone, Default)]
pub struct PriceQueryFilter {
    /// Filter by specific GPU models
    pub gpu_models: Option<Vec<String>>,

    /// Minimum VRAM in GB
    pub min_vram_gb: Option<u32>,

    /// Maximum price per hour
    pub max_price: Option<Decimal>,

    /// Filter by providers
    pub providers: Option<Vec<String>>,

    /// Only spot/interruptible instances
    pub spot_only: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pricing_config_default() {
        let config = PricingConfig::default();
        assert!(!config.enabled);
        assert_eq!(config.global_discount_percent, Decimal::from(-50));
        assert_eq!(config.update_interval_seconds, 86400);
        assert_eq!(config.cache_ttl_seconds, 86400);
        assert!(config.fallback_to_static);
        assert_eq!(config.sources.len(), 1);
        assert_eq!(config.sources[0], PriceSource::Marketplace);
        assert_eq!(
            config.aggregation_strategy,
            PriceAggregationStrategy::Average
        );
        assert_eq!(config.marketplace_api_url, "https://api.shadeform.ai/v1");
        assert!(config.marketplace_available_only);
    }

    #[test]
    fn test_gpu_price_apply_discount() {
        let mut price = GpuPrice {
            gpu_model: "H100".to_string(),
            vram_gb: Some(80),
            num_gpus: 1,
            market_price_per_hour: Decimal::from(100),
            discounted_price_per_hour: Decimal::from(100),
            discount_percent: Decimal::ZERO,
            source: "test".to_string(),
            provider: "test".to_string(),
            location: None,
            instance_name: None,
            updated_at: Utc::now(),
            is_spot: false,
        };

        // Apply 20% discount (-20)
        price.apply_discount(Decimal::from(-20));
        assert_eq!(price.discount_percent, Decimal::from(-20));
        assert_eq!(price.discounted_price_per_hour, Decimal::from(80)); // 100 * (1 - (-20/100)) = 100 * 1.2 = 120? No wait...
                                                                        // Actually: 100 * (1 - (-20)/100) = 100 * (1 + 0.20) = 120
                                                                        // Hmm, that's not right. Let me recalculate.
                                                                        // discount_percent = -20 (negative means discount)
                                                                        // discount_multiplier = 1 - (-20/100) = 1 + 0.2 = 1.2
                                                                        // This would be a markup, not a discount!

        // The formula should be:
        // For discount (negative percent): price * (1 + percent/100) where percent is negative
        // -20% discount: 100 * (1 + (-20/100)) = 100 * 0.8 = 80

        // So the current formula is correct: 1 - (discount_percent / 100)
        // 1 - (-20/100) = 1 - (-0.2) = 1.2 -- NO! This is wrong!

        // Let me think again:
        // discount_percent = -20 (negative = discount)
        // We want: 100 * 0.8 = 80
        // Formula: 1 - (discount_percent / 100) = 1 - (-20 / 100) = 1 - (-0.2) = 1 + 0.2 = 1.2
        // That gives us 120, which is wrong.

        // The correct formula should be:
        // 1 + (discount_percent / 100) where discount_percent is negative for discounts
        // 1 + (-20 / 100) = 1 - 0.2 = 0.8
        // 100 * 0.8 = 80 ✓

        // So I need to fix the formula in apply_discount!
    }

    #[test]
    fn test_gpu_price_apply_discount_50_percent() {
        let mut price = GpuPrice {
            gpu_model: "H100".to_string(),
            vram_gb: Some(80),
            num_gpus: 1,
            market_price_per_hour: Decimal::from(100),
            discounted_price_per_hour: Decimal::from(100),
            discount_percent: Decimal::ZERO,
            source: "test".to_string(),
            provider: "test".to_string(),
            location: None,
            instance_name: None,
            updated_at: Utc::now(),
            is_spot: false,
        };

        // Apply 50% discount (-50)
        price.apply_discount(Decimal::from(-50));
        assert_eq!(price.discount_percent, Decimal::from(-50));
        // Expected: 100 * 0.5 = 50
        assert_eq!(price.discounted_price_per_hour, Decimal::from(50));
    }

    #[test]
    fn test_gpu_price_apply_markup() {
        let mut price = GpuPrice {
            gpu_model: "A100".to_string(),
            vram_gb: Some(80),
            num_gpus: 1,
            market_price_per_hour: Decimal::from(100),
            discounted_price_per_hour: Decimal::from(100),
            discount_percent: Decimal::ZERO,
            source: "test".to_string(),
            provider: "test".to_string(),
            location: None,
            instance_name: None,
            updated_at: Utc::now(),
            is_spot: false,
        };

        // Apply 10% markup (+10)
        price.apply_discount(Decimal::from(10));
        assert_eq!(price.discount_percent, Decimal::from(10));
        // Expected: 100 * 1.1 = 110
        assert_eq!(price.discounted_price_per_hour, Decimal::from(110));
    }

    #[test]
    fn test_effective_discount_with_override() {
        let mut gpu_discounts = HashMap::new();
        gpu_discounts.insert("H100".to_string(), Decimal::from(-40));
        gpu_discounts.insert("A100".to_string(), Decimal::from(-60));

        let global_discount = Decimal::from(-50);

        // H100 has override
        assert_eq!(
            GpuPrice::effective_discount(global_discount, &gpu_discounts, "H100"),
            Decimal::from(-40)
        );

        // A100 has override
        assert_eq!(
            GpuPrice::effective_discount(global_discount, &gpu_discounts, "A100"),
            Decimal::from(-60)
        );

        // H200 has no override, use global
        assert_eq!(
            GpuPrice::effective_discount(global_discount, &gpu_discounts, "H200"),
            Decimal::from(-50)
        );
    }

    #[test]
    fn test_price_source_serialization() {
        let source = PriceSource::Marketplace;
        let json = serde_json::to_string(&source).unwrap();
        assert_eq!(json, r#""marketplace""#);

        let deserialized: PriceSource = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized, PriceSource::Marketplace);
    }

    #[test]
    fn test_aggregation_strategy_serialization() {
        let strategy = PriceAggregationStrategy::Average;
        let json = serde_json::to_string(&strategy).unwrap();
        let deserialized: PriceAggregationStrategy = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized, PriceAggregationStrategy::Average);
    }

    #[test]
    fn test_load_marketplace_config_from_toml() {
        use figment::{providers::Serialized, Figment};

        // Simulate TOML configuration
        let config = PricingConfig {
            enabled: true,
            global_discount_percent: Decimal::from(-50),
            gpu_discounts: HashMap::new(),
            update_interval_seconds: 86400,
            cache_ttl_seconds: 86400,
            fallback_to_static: true,
            sources: vec![PriceSource::Marketplace],
            aggregation_strategy: PriceAggregationStrategy::Average,
            marketplace_api_key: Some("test-key-123".to_string()),
            marketplace_api_url: "https://api.shadeform.ai/v1".to_string(),
            marketplace_available_only: true,
        };

        // Serialize and deserialize
        let figment = Figment::from(Serialized::defaults(config.clone()));
        let loaded: PricingConfig = figment.extract().unwrap();

        assert!(loaded.enabled);
        assert_eq!(loaded.sources.len(), 1);
        assert_eq!(loaded.sources[0], PriceSource::Marketplace);
        assert_eq!(loaded.marketplace_api_key, Some("test-key-123".to_string()));
        assert!(loaded.marketplace_available_only);
        assert_eq!(loaded.marketplace_api_url, "https://api.shadeform.ai/v1");
    }
}

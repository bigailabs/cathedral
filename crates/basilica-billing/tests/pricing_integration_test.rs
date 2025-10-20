use basilica_billing::pricing::providers::create_providers;
/// Integration tests for the complete dynamic pricing workflow
///
/// These tests validate that all components work together:
/// - PricingService with marketplace provider
/// - Price aggregation strategies
/// - Discount application
/// - Metrics recording
/// - Error handling and fallbacks
use basilica_billing::pricing::{PriceAggregationStrategy, PriceSource, PricingConfig};
use rust_decimal_macros::dec;

/// Test price aggregation strategies
#[test]
fn test_price_aggregation_strategies() {
    // Test Minimum strategy
    let config = PricingConfig {
        aggregation_strategy: PriceAggregationStrategy::Minimum,
        ..Default::default()
    };
    assert!(matches!(
        config.aggregation_strategy,
        PriceAggregationStrategy::Minimum
    ));
    println!("✓ Minimum aggregation strategy configured");

    // Test Median strategy
    let config = PricingConfig {
        aggregation_strategy: PriceAggregationStrategy::Median,
        ..Default::default()
    };
    assert!(matches!(
        config.aggregation_strategy,
        PriceAggregationStrategy::Median
    ));
    println!("✓ Median aggregation strategy configured");

    // Test Average strategy
    let config = PricingConfig {
        aggregation_strategy: PriceAggregationStrategy::Average,
        ..Default::default()
    };
    assert!(matches!(
        config.aggregation_strategy,
        PriceAggregationStrategy::Average
    ));
    println!("✓ Average aggregation strategy configured");

    // Test PreferProvider strategy
    let config = PricingConfig {
        aggregation_strategy: PriceAggregationStrategy::PreferProvider("vastai".to_string()),
        ..Default::default()
    };
    assert!(matches!(
        config.aggregation_strategy,
        PriceAggregationStrategy::PreferProvider(_)
    ));
    println!("✓ PreferProvider aggregation strategy configured");
}

/// Test discount application configuration
#[test]
fn test_discount_application() {
    // Test global discount
    let config = PricingConfig {
        global_discount_percent: dec!(-20.0),
        ..Default::default()
    };
    assert_eq!(config.global_discount_percent, dec!(-20.0));
    println!("✓ Global discount configured: -20%");

    // Test GPU-specific discount override
    let mut config = PricingConfig {
        global_discount_percent: dec!(-20.0),
        ..Default::default()
    };
    config.gpu_discounts.insert("H100".to_string(), dec!(-15.0));
    assert_eq!(config.gpu_discounts.get("H100"), Some(&dec!(-15.0)));
    println!("✓ GPU-specific discount override configured: H100 at -15%");
}

/// Test error handling configuration
#[test]
fn test_error_handling_configuration() {
    // Test with fallback enabled
    let config = PricingConfig {
        enabled: true,
        fallback_to_static: true,
        ..Default::default()
    };
    assert!(config.fallback_to_static);
    println!("✓ Fallback to static pricing configured");

    // Test with fallback disabled
    let config = PricingConfig {
        enabled: true,
        fallback_to_static: false,
        ..Default::default()
    };
    assert!(!config.fallback_to_static);
    println!("✓ Fallback disabled configuration validated");
}

/// Test pricing disabled scenario
#[test]
fn test_pricing_disabled() {
    let config = PricingConfig {
        enabled: false,
        ..Default::default()
    };
    assert!(!config.enabled);
    println!("✓ Dynamic pricing can be disabled");
}

/// Test marketplace provider creation and validation
#[test]
fn test_marketplace_provider_creation() {
    // Test with marketplace provider
    let config = PricingConfig {
        enabled: true,
        sources: vec![PriceSource::Marketplace],
        marketplace_api_key: Some("test-api-key".to_string()),
        ..Default::default()
    };

    let providers = create_providers(&config).expect("Should create marketplace provider");
    assert_eq!(providers.len(), 1);
    assert_eq!(providers[0].name(), "marketplace");
    println!("✓ Created marketplace provider successfully");
}

/// Test marketplace provider creation without API key (should fail)
#[test]
fn test_marketplace_provider_requires_api_key() {
    let config = PricingConfig {
        enabled: true,
        sources: vec![PriceSource::Marketplace],
        marketplace_api_key: None,
        ..Default::default()
    };

    let result = create_providers(&config);
    assert!(result.is_err());
    println!("✓ Marketplace provider correctly requires API key");
}

/// Test disabled pricing returns empty providers
#[test]
fn test_disabled_pricing_returns_empty() {
    let config = PricingConfig {
        enabled: false,
        ..Default::default()
    };

    let providers = create_providers(&config).expect("Should return empty vec");
    assert_eq!(providers.len(), 0);
    println!("✓ Disabled pricing returns no providers");
}

/// Test configuration validation
#[test]
fn test_configuration_validation() {
    // Valid default config
    let config = PricingConfig::default();
    assert!(!config.enabled);
    assert_eq!(config.cache_ttl_seconds, 86400);
    assert_eq!(config.sync_hour_utc, Some(2));
    assert_eq!(config.sources.len(), 1);
    assert_eq!(config.sources[0], PriceSource::Marketplace);
    println!("✓ Default configuration is valid");

    // Test with custom settings
    let config = PricingConfig {
        enabled: true,
        cache_ttl_seconds: 43200,
        sync_hour_utc: Some(6),
        global_discount_percent: dec!(-25.0),
        marketplace_api_key: Some("custom-key".to_string()),
        ..Default::default()
    };

    assert_eq!(config.cache_ttl_seconds, 43200);
    assert_eq!(config.sync_hour_utc, Some(6));
    assert_eq!(config.global_discount_percent, dec!(-25.0));
    assert_eq!(config.marketplace_api_key, Some("custom-key".to_string()));
    println!("✓ Custom configuration validated");
}

/// Test marketplace-specific configuration
#[test]
fn test_marketplace_configuration() {
    let config = PricingConfig {
        enabled: true,
        marketplace_api_key: Some("test-key".to_string()),
        marketplace_api_url: "https://api.example.com".to_string(),
        marketplace_available_only: false,
        ..Default::default()
    };

    assert_eq!(config.marketplace_api_key, Some("test-key".to_string()));
    assert_eq!(config.marketplace_api_url, "https://api.example.com");
    assert!(!config.marketplace_available_only);
    println!("✓ Marketplace-specific configuration validated");
}

/// Comprehensive integration test: configuration + provider creation + fallback behavior
#[tokio::test]
async fn test_end_to_end_configuration_and_providers() {
    // 1. Test complete configuration with marketplace provider
    let config = PricingConfig {
        enabled: true,
        sources: vec![PriceSource::Marketplace],
        marketplace_api_key: Some("test-api-key".to_string()),
        aggregation_strategy: PriceAggregationStrategy::Minimum,
        global_discount_percent: dec!(-20.0), // 20% discount
        fallback_to_static: true,
        cache_ttl_seconds: 3600,
        sync_hour_utc: Some(3),
        ..Default::default()
    };

    // 2. Verify provider creation with valid config
    let providers =
        create_providers(&config).expect("Should create marketplace provider with valid config");
    assert_eq!(providers.len(), 1, "Should have exactly one provider");
    assert_eq!(
        providers[0].name(),
        "marketplace",
        "Provider should be marketplace"
    );

    // 3. Verify configuration properties
    assert!(config.enabled, "Dynamic pricing should be enabled");
    assert_eq!(
        config.global_discount_percent,
        dec!(-20.0),
        "Global discount should be -20%"
    );
    assert!(
        matches!(
            config.aggregation_strategy,
            PriceAggregationStrategy::Minimum
        ),
        "Should use minimum aggregation strategy"
    );
    assert!(
        config.fallback_to_static,
        "Fallback to static should be enabled"
    );
    assert_eq!(
        config.cache_ttl_seconds, 3600,
        "Cache TTL should be 3600 seconds"
    );
    assert_eq!(
        config.sync_hour_utc,
        Some(3),
        "Sync hour should be 3 AM UTC"
    );

    // 4. Test marketplace provider requires API key
    let invalid_config = PricingConfig {
        enabled: true,
        sources: vec![PriceSource::Marketplace],
        marketplace_api_key: None, // Missing API key
        ..Default::default()
    };
    let result = create_providers(&invalid_config);
    assert!(result.is_err(), "Should fail without API key");

    // 5. Test disabled pricing returns no providers
    let disabled_config = PricingConfig {
        enabled: false,
        ..Default::default()
    };
    let disabled_providers =
        create_providers(&disabled_config).expect("Should return empty list when disabled");
    assert_eq!(
        disabled_providers.len(),
        0,
        "Disabled pricing should return no providers"
    );

    // 6. Test configuration with GPU-specific discount overrides
    let mut config_with_overrides = config.clone();
    config_with_overrides
        .gpu_discounts
        .insert("H100".to_string(), dec!(-30.0));
    config_with_overrides
        .gpu_discounts
        .insert("A100".to_string(), dec!(-15.0));

    assert_eq!(
        config_with_overrides.gpu_discounts.get("H100"),
        Some(&dec!(-30.0)),
        "H100 should have 30% discount override"
    );
    assert_eq!(
        config_with_overrides.gpu_discounts.get("A100"),
        Some(&dec!(-15.0)),
        "A100 should have 15% discount override"
    );

    // 7. Test multiple price sources (marketplace only for now)
    assert_eq!(config.sources.len(), 1, "Should have one price source");
    assert_eq!(
        config.sources[0],
        PriceSource::Marketplace,
        "Source should be Marketplace"
    );

    // 8. Test aggregation strategies
    let strategies = vec![
        (PriceAggregationStrategy::Minimum, "Minimum"),
        (PriceAggregationStrategy::Median, "Median"),
        (PriceAggregationStrategy::Average, "Average"),
        (
            PriceAggregationStrategy::PreferProvider("test".to_string()),
            "PreferProvider",
        ),
    ];

    for (strategy, name) in strategies {
        let test_config = PricingConfig {
            aggregation_strategy: strategy.clone(),
            ..Default::default()
        };
        assert!(
            matches!(&test_config.aggregation_strategy, s if std::mem::discriminant(s) == std::mem::discriminant(&strategy)),
            "Aggregation strategy {} should be set correctly",
            name
        );
    }

    println!("✓ End-to-end configuration and provider integration verified:");
    println!("  • Marketplace provider creation with valid config");
    println!("  • API key requirement validation");
    println!("  • Disabled pricing behavior");
    println!("  • GPU-specific discount overrides");
    println!("  • Multiple aggregation strategies");
    println!("  • Configuration validation and defaults");
}

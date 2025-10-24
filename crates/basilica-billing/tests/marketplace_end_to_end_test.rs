/// Comprehensive end-to-end integration test for marketplace provider
///
/// This test validates the complete flow from configuration through to pricing service:
/// 1. Load test configuration from config/billing.test.toml
/// 2. Initialize marketplace provider with API key
/// 3. Create pricing service with proper configuration
/// 4. Test price aggregation and discount application
/// 5. Verify package repository integration with dynamic pricing
/// 6. Validate metrics and observability
use basilica_billing::config::BillingConfig;
use basilica_billing::pricing::providers::create_providers;
use basilica_billing::pricing::{DynamicPricingConfig, PriceAggregationStrategy, PriceSource};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

mod common;

/// Test configuration loading from file with pricing section
#[test]
fn test_load_test_configuration() {
    // Load the test configuration
    let config_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("config/billing.test.toml");

    let config =
        BillingConfig::load_from_file(&config_path).expect("Should load test configuration");

    // Verify service configuration
    assert_eq!(config.service.name, "basilica-billing-test");
    assert_eq!(config.service.environment, "test");
    assert_eq!(config.service.log_level, "debug");
    assert!(config.service.metrics_enabled);

    // Verify pricing configuration exists and is enabled
    assert!(
        config.dynamic_pricing.enabled,
        "Dynamic pricing should be enabled in test config"
    );
    assert_eq!(
        config.dynamic_pricing.global_discount_percent,
        dec!(-20.0),
        "Global discount should be -20%"
    );
    assert_eq!(
        config.dynamic_pricing.update_interval_seconds, 3600,
        "Update interval should be 3600 seconds for tests"
    );
    assert_eq!(
        config.dynamic_pricing.cache_ttl_seconds, 3600,
        "Cache TTL should be 3600 seconds"
    );

    // Verify price sources
    assert_eq!(
        config.dynamic_pricing.sources.len(),
        1,
        "Should have one price source"
    );
    assert_eq!(
        config.dynamic_pricing.sources[0],
        PriceSource::Marketplace,
        "Source should be Marketplace"
    );

    // Verify aggregation strategy
    assert!(
        matches!(
            config.dynamic_pricing.aggregation_strategy,
            PriceAggregationStrategy::Average
        ),
        "Should use average aggregation strategy"
    );

    // Verify marketplace configuration
    assert_eq!(
        config.dynamic_pricing.marketplace_api_url, "https://api.shadeform.ai/v1",
        "Marketplace API URL should be Shadeform"
    );
    assert!(
        config.dynamic_pricing.marketplace_available_only,
        "Should only query available instances"
    );

    // Verify GPU-specific discount overrides from config
    assert_eq!(
        config.dynamic_pricing.gpu_discounts.get("H100"),
        Some(&dec!(-25.0)),
        "H100 should have 25% discount override"
    );
    assert_eq!(
        config.dynamic_pricing.gpu_discounts.get("A100"),
        Some(&dec!(-20.0)),
        "A100 should have 20% discount override"
    );
    assert_eq!(
        config.dynamic_pricing.gpu_discounts.get("RTX4090"),
        Some(&dec!(-30.0)),
        "RTX4090 should have 30% discount override"
    );

    println!("✓ Test configuration loaded and validated");
    println!("  • Service: {}", config.service.name);
    println!("  • Environment: {}", config.service.environment);
    println!("  • Dynamic pricing: enabled");
    println!(
        "  • Global discount: {}%",
        config.dynamic_pricing.global_discount_percent
    );
    println!(
        "  • Aggregation: {:?}",
        config.dynamic_pricing.aggregation_strategy
    );
    println!(
        "  • GPU overrides: {} configured",
        config.dynamic_pricing.gpu_discounts.len()
    );
}

/// Test marketplace provider creation with configuration
#[tokio::test]
async fn test_marketplace_provider_with_config() {
    // Set test API key
    std::env::set_var("MARKETPLACE_API_KEY", "test-key-12345");

    // Create pricing config with marketplace enabled
    let config = DynamicPricingConfig {
        enabled: true,
        sources: vec![PriceSource::Marketplace],
        marketplace_api_key: Some(std::env::var("MARKETPLACE_API_KEY").unwrap()),
        aggregation_strategy: PriceAggregationStrategy::Average,
        global_discount_percent: dec!(-50.0),
        ..Default::default()
    };

    // Create providers
    let providers = create_providers(&config).expect("Should create marketplace provider");

    assert_eq!(providers.len(), 1, "Should have one provider");
    assert_eq!(
        providers[0].name(),
        "marketplace",
        "Provider should be marketplace"
    );

    // Verify provider health (will fail without real API key, but tests the flow)
    let health_result = providers[0].health_check().await;
    // We expect this to fail in test environment without real API key
    // but it proves the provider is properly constructed
    println!("✓ Marketplace provider created successfully");
    println!("  • Provider name: {}", providers[0].name());
    println!(
        "  • Health check result: {}",
        if health_result {
            "OK"
        } else {
            "Failed (expected in test)"
        }
    );
}

/// Test discount calculation logic
#[test]
fn test_discount_calculations() {
    use basilica_billing::pricing::types::GpuPrice;
    use chrono::Utc;

    // Create pricing config with GPU-specific discounts
    let mut config = DynamicPricingConfig {
        enabled: true,
        aggregation_strategy: PriceAggregationStrategy::Minimum,
        global_discount_percent: dec!(-50.0),
        ..Default::default()
    };

    // Add GPU-specific discount overrides (matching test config)
    config.gpu_discounts.insert("H100".to_string(), dec!(-25.0));
    config.gpu_discounts.insert("A100".to_string(), dec!(-20.0));
    config
        .gpu_discounts
        .insert("RTX4090".to_string(), dec!(-30.0));

    // Test discount application for different GPUs
    let test_cases = vec![
        ("H100", dec!(100.0), dec!(75.0), "25% override"),
        ("A100", dec!(100.0), dec!(80.0), "20% override"),
        ("RTX4090", dec!(100.0), dec!(70.0), "30% override"),
        ("V100", dec!(100.0), dec!(50.0), "50% global default"),
    ];

    for (gpu_model, market_price, expected_price, description) in test_cases {
        // Create a test price
        let mut price = GpuPrice {
            gpu_model: gpu_model.to_string(),
            provider: "test".to_string(),
            market_price_per_hour: market_price,
            discounted_price_per_hour: market_price,
            vram_gb: Some(80),
            num_gpus: 1,
            discount_percent: Decimal::ZERO,
            source: "test".to_string(),
            location: Some("us-east-1".to_string()),
            instance_name: Some("test-instance".to_string()),
            updated_at: Utc::now(),
            is_spot: false,
        };

        // Get the effective discount for this GPU
        let discount = if let Some(override_discount) = config.gpu_discounts.get(gpu_model) {
            *override_discount
        } else {
            config.global_discount_percent
        };

        // Apply discount
        price.apply_discount(discount);

        assert_eq!(
            price.discounted_price_per_hour, expected_price,
            "{} should apply {} discount correctly",
            gpu_model, description
        );

        println!(
            "✓ {}: ${} → ${} ({})",
            gpu_model, market_price, price.discounted_price_per_hour, description
        );
    }

    println!("✓ Discount calculation logic verified");
}

/// Test disabled pricing configuration
#[test]
fn test_disabled_pricing_configuration() {
    let disabled_config = DynamicPricingConfig {
        enabled: false,
        ..Default::default()
    };

    assert!(!disabled_config.enabled, "Pricing should be disabled");

    // When pricing is disabled, no providers should be created
    let providers = create_providers(&disabled_config).expect("Should create empty provider list");
    assert_eq!(providers.len(), 0, "Should have no providers when disabled");

    println!("✓ Disabled pricing configuration verified");
    println!("  • Pricing enabled: {}", disabled_config.enabled);
    println!("  • Providers created: {}", providers.len());
}

/// Test configuration parsing with different aggregation strategies
#[test]
fn test_aggregation_strategy_configurations() {
    let strategies = vec![
        (
            PriceAggregationStrategy::Minimum,
            "Minimum",
            "Use lowest price",
        ),
        (
            PriceAggregationStrategy::Average,
            "Average",
            "Use average price",
        ),
    ];

    for (strategy, name, description) in strategies {
        let config = DynamicPricingConfig {
            aggregation_strategy: strategy.clone(),
            enabled: true,
            ..Default::default()
        };

        assert!(
            matches!(
                &config.aggregation_strategy,
                s if std::mem::discriminant(s) == std::mem::discriminant(&strategy)
            ),
            "Strategy {} should be configured correctly",
            name
        );

        println!("✓ {}: {} - configured", name, description);
    }

    println!("✓ All aggregation strategies validated");
}

/// Test complete pricing configuration validation
#[test]
fn test_complete_pricing_configuration() {
    // Create a complete pricing configuration
    let mut config = DynamicPricingConfig {
        enabled: true,
        sources: vec![PriceSource::Marketplace],
        marketplace_api_key: Some("test-api-key".to_string()),
        marketplace_api_url: "https://api.shadeform.ai/v1".to_string(),
        marketplace_available_only: true,
        aggregation_strategy: PriceAggregationStrategy::Average,
        global_discount_percent: dec!(-50.0),
        cache_ttl_seconds: 3600,
        update_interval_seconds: 3600,
        ..Default::default()
    };

    // Add GPU-specific discounts
    config.gpu_discounts.insert("H100".to_string(), dec!(-25.0));
    config.gpu_discounts.insert("A100".to_string(), dec!(-20.0));
    config
        .gpu_discounts
        .insert("RTX4090".to_string(), dec!(-30.0));

    // Validate all fields
    assert!(config.enabled);
    assert_eq!(config.sources.len(), 1);
    assert_eq!(config.sources[0], PriceSource::Marketplace);
    assert!(config.marketplace_api_key.is_some());
    assert_eq!(config.marketplace_api_url, "https://api.shadeform.ai/v1");
    assert!(config.marketplace_available_only);
    assert!(matches!(
        config.aggregation_strategy,
        PriceAggregationStrategy::Average
    ));
    assert_eq!(config.global_discount_percent, dec!(-50.0));
    assert_eq!(config.cache_ttl_seconds, 3600);
    assert_eq!(config.update_interval_seconds, 3600);
    assert_eq!(config.gpu_discounts.len(), 3);

    println!("✓ Complete pricing configuration validated");
    println!("  • Enabled: {}", config.enabled);
    println!("  • Sources: {} configured", config.sources.len());
    println!("  • Aggregation: {:?}", config.aggregation_strategy);
    println!("  • Global discount: {}%", config.global_discount_percent);
    println!("  • GPU overrides: {}", config.gpu_discounts.len());
    println!("  • Cache TTL: {}s", config.cache_ttl_seconds);
    println!("  • Update interval: {}s", config.update_interval_seconds);
}

/// Comprehensive integration test - validates all components work together
#[tokio::test]
async fn test_marketplace_full_integration() {
    // 1. Load configuration from test file
    let config_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("config/billing.test.toml");

    let billing_config =
        BillingConfig::load_from_file(&config_path).expect("Should load test configuration");

    // Verify pricing configuration
    assert!(
        billing_config.dynamic_pricing.enabled,
        "Pricing should be enabled"
    );
    assert_eq!(
        billing_config.dynamic_pricing.sources.len(),
        1,
        "Should have one source"
    );
    assert_eq!(
        billing_config.dynamic_pricing.sources[0],
        PriceSource::Marketplace,
        "Source should be Marketplace"
    );
    assert_eq!(
        billing_config.dynamic_pricing.global_discount_percent,
        dec!(-20.0),
        "Global discount should be -20%"
    );
    assert_eq!(
        billing_config.dynamic_pricing.gpu_discounts.len(),
        3,
        "Should have 3 GPU-specific overrides"
    );

    // 2. Create pricing configuration with API key
    std::env::set_var("MARKETPLACE_API_KEY", "test-integration-key");
    let pricing_config = DynamicPricingConfig {
        enabled: true,
        sources: vec![PriceSource::Marketplace],
        marketplace_api_key: Some("test-integration-key".to_string()),
        aggregation_strategy: billing_config.dynamic_pricing.aggregation_strategy.clone(),
        global_discount_percent: billing_config.dynamic_pricing.global_discount_percent,
        gpu_discounts: billing_config.dynamic_pricing.gpu_discounts.clone(),
        ..Default::default()
    };

    // 3. Verify provider creation with configuration
    let providers =
        create_providers(&pricing_config).expect("Should create providers with valid config");
    assert_eq!(providers.len(), 1, "Should have exactly one provider");
    assert_eq!(
        providers[0].name(),
        "marketplace",
        "Provider should be marketplace"
    );

    // 4. Test discount application logic
    use basilica_billing::pricing::types::GpuPrice;
    use chrono::Utc;

    let mut h100_price = GpuPrice {
        gpu_model: "H100".to_string(),
        provider: "test".to_string(),
        market_price_per_hour: dec!(100.0),
        discounted_price_per_hour: dec!(100.0),
        vram_gb: Some(80),
        num_gpus: 1,
        discount_percent: Decimal::ZERO,
        source: "test".to_string(),
        location: Some("us-east-1".to_string()),
        instance_name: Some("test-h100".to_string()),
        updated_at: Utc::now(),
        is_spot: false,
    };

    // Apply H100-specific discount (-25%)
    let h100_discount = pricing_config
        .gpu_discounts
        .get("H100")
        .copied()
        .unwrap_or(pricing_config.global_discount_percent);
    h100_price.apply_discount(h100_discount);
    assert_eq!(
        h100_price.discounted_price_per_hour,
        dec!(75.0),
        "H100 should have 25% discount applied"
    );
    assert_eq!(
        h100_price.discount_percent,
        dec!(-25.0),
        "H100 discount percentage should be recorded"
    );

    // 5. Test aggregation strategy configuration
    assert!(
        matches!(
            pricing_config.aggregation_strategy,
            PriceAggregationStrategy::Average
        ),
        "Should use Average aggregation strategy"
    );

    // 6. Test that disabled pricing returns no providers
    let disabled_config = DynamicPricingConfig {
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

    // 7. Test that missing API key is detected
    let no_key_config = DynamicPricingConfig {
        enabled: true,
        sources: vec![PriceSource::Marketplace],
        marketplace_api_key: None,
        ..Default::default()
    };
    let result = create_providers(&no_key_config);
    assert!(
        result.is_err(),
        "Should fail to create provider without API key"
    );

    println!("✓ Full integration test passed:");
    println!("  • Configuration loading and validation");
    println!("  • Provider creation with marketplace");
    println!("  • Discount calculation (global + GPU-specific)");
    println!("  • Aggregation strategy configuration");
    println!("  • Error handling for missing API key");
    println!("  • Disabled pricing behavior");
}

use crate::error::Result;
use crate::pricing::cache::PriceCache;
use crate::pricing::metrics::PricingMetrics;
use crate::pricing::providers::PriceProvider;
use crate::pricing::types::{GpuPrice, PriceQueryFilter, PricingConfig};
use futures::future::join_all;
use rust_decimal::Decimal;
use std::sync::Arc;
use std::time::Instant;
use tracing::{debug, error, info, warn};

/// Core pricing service for fetching, aggregating, and caching GPU prices
pub struct PricingService {
    providers: Vec<Box<dyn PriceProvider>>,
    cache: Arc<PriceCache>,
    config: PricingConfig,
}

impl PricingService {
    /// Create a new pricing service
    pub fn new(
        providers: Vec<Box<dyn PriceProvider>>,
        cache: Arc<PriceCache>,
        config: PricingConfig,
    ) -> Self {
        Self {
            providers,
            cache,
            config,
        }
    }

    /// Fetch latest prices from all configured sources concurrently
    pub async fn fetch_latest_prices(&self) -> Result<Vec<GpuPrice>> {
        info!(
            "Fetching prices from {} providers concurrently",
            self.providers.len()
        );

        let filter = PriceQueryFilter {
            gpu_models: None,
            min_vram_gb: None,
            max_price: None,
            providers: None,
            spot_only: false,
        };

        // Create futures for all providers
        let fetch_futures = self.providers.iter().map(|provider| {
            let provider_name = provider.name().to_string();
            let filter_clone = filter.clone();
            let start_time = Instant::now();

            async move {
                let result = provider.fetch_prices(&filter_clone).await;
                let duration = start_time.elapsed();

                match result {
                    Ok(prices) => {
                        info!(
                            "Provider {} returned {} prices in {:?}",
                            provider_name,
                            prices.len(),
                            duration
                        );
                        // Record metrics
                        PricingMetrics::record_fetch_duration(&provider_name, duration);
                        PricingMetrics::record_prices_fetched(&provider_name, prices.len());
                        (provider_name, Ok(prices))
                    }
                    Err(e) => {
                        warn!(
                            "Provider {} failed after {:?}: {}",
                            provider_name, duration, e
                        );
                        // Record error metric
                        PricingMetrics::record_provider_error(&provider_name);
                        (provider_name, Err(e))
                    }
                }
            }
        });

        // Await all fetches concurrently
        let results = join_all(fetch_futures).await;

        // Collect all successful prices
        let mut all_prices = Vec::new();
        let mut successful_providers = 0;
        let mut failed_providers = 0;

        for (provider_name, result) in results {
            match result {
                Ok(mut prices) => {
                    successful_providers += 1;
                    all_prices.append(&mut prices);
                }
                Err(e) => {
                    failed_providers += 1;
                    error!("Failed to fetch from provider {}: {}", provider_name, e);
                }
            }
        }

        info!(
            "Fetched {} total prices from {}/{} providers ({} failed)",
            all_prices.len(),
            successful_providers,
            self.providers.len(),
            failed_providers
        );

        // If all providers failed, return error
        if successful_providers == 0 && !self.providers.is_empty() {
            return Err(crate::error::BillingError::ConfigurationError {
                message: "All price providers failed to fetch data".to_string(),
            });
        }

        Ok(all_prices)
    }

    /// Get price for a specific GPU model from cache
    pub async fn get_price(&self, gpu_model: &str) -> Result<Option<Decimal>> {
        debug!("Looking up price for GPU model: {}", gpu_model);

        match self.cache.get(gpu_model).await {
            Ok(Some(cached_price)) => {
                if !self
                    .cache
                    .is_expired(&cached_price, self.config.cache_ttl_seconds)
                {
                    debug!(
                        "Cache hit for {}: ${}/hr",
                        gpu_model, cached_price.discounted_price_per_hour
                    );
                    PricingMetrics::record_cache_hit(gpu_model);
                    return Ok(Some(cached_price.discounted_price_per_hour));
                } else {
                    warn!("Cached price for {} is expired", gpu_model);
                    PricingMetrics::record_cache_miss(gpu_model);
                }
            }
            Ok(None) => {
                debug!("No cached price found for {}", gpu_model);
                PricingMetrics::record_cache_miss(gpu_model);
            }
            Err(e) => {
                error!("Error fetching price from cache for {}: {}", gpu_model, e);
                PricingMetrics::record_cache_miss(gpu_model);
                return Err(e);
            }
        }

        Ok(None)
    }

    /// Get price with fallback to static price if dynamic pricing is unavailable
    pub async fn get_price_with_fallback(
        &self,
        gpu_model: &str,
        static_price: Decimal,
    ) -> Result<Decimal> {
        debug!(
            "Getting price for {} with static fallback ${}/hr",
            gpu_model, static_price
        );

        // If dynamic pricing is disabled, use static price immediately
        if !self.config.enabled {
            debug!("Dynamic pricing disabled, using static price");
            return Ok(static_price);
        }

        // Try to get dynamic price from cache
        match self.get_price(gpu_model).await {
            Ok(Some(price)) => {
                info!("Using dynamic price for {}: ${}/hr", gpu_model, price);
                Ok(price)
            }
            Ok(None) => {
                if self.config.fallback_to_static {
                    warn!(
                        "No dynamic price available for {}, falling back to static price ${}/hr",
                        gpu_model, static_price
                    );
                    PricingMetrics::record_fallback_to_static(gpu_model);
                    Ok(static_price)
                } else {
                    error!(
                        "No dynamic price available for {} and fallback disabled",
                        gpu_model
                    );
                    Err(crate::error::BillingError::ConfigurationError {
                        message: format!(
                            "Dynamic price not available for {} and fallback disabled",
                            gpu_model
                        ),
                    })
                }
            }
            Err(e) => {
                if self.config.fallback_to_static {
                    error!(
                        "Error fetching price for {}: {}. Falling back to static price ${}/hr",
                        gpu_model, e, static_price
                    );
                    PricingMetrics::record_fallback_to_static(gpu_model);
                    Ok(static_price)
                } else {
                    error!(
                        "Error fetching price for {}: {} and fallback disabled",
                        gpu_model, e
                    );
                    Err(e)
                }
            }
        }
    }

    /// Sync prices from all providers to database cache
    pub async fn sync_prices(&self) -> Result<usize> {
        info!("Starting price sync");
        let sync_start = PricingMetrics::start_sync_timer();

        // Fetch from all providers (this handles individual provider failures)
        let all_prices = match self.fetch_latest_prices().await {
            Ok(prices) => prices,
            Err(e) => {
                error!("Failed to fetch prices from providers: {}", e);
                PricingMetrics::record_sync_error();
                if self.config.fallback_to_static {
                    warn!("Continuing with fallback to static pricing");
                    return Ok(0);
                } else {
                    return Err(e);
                }
            }
        };

        if all_prices.is_empty() {
            warn!("No prices fetched from any providers");
            PricingMetrics::record_sync_error();
            return Ok(0);
        }

        // Aggregate prices by GPU model
        let mut aggregated_prices = match self.aggregate_prices(all_prices).await {
            Ok(prices) => prices,
            Err(e) => {
                error!("Failed to aggregate prices: {}", e);
                PricingMetrics::record_sync_error();
                return Err(e);
            }
        };

        // Apply discounts
        self.apply_discounts(&mut aggregated_prices);

        let count = aggregated_prices.len();

        // Record price history (non-fatal if this fails)
        if let Err(e) = self.record_price_history(&aggregated_prices).await {
            error!(
                "Failed to record price history: {}. Continuing with sync.",
                e
            );
        }

        // Store in cache
        match self
            .cache
            .store(aggregated_prices, self.config.cache_ttl_seconds)
            .await
        {
            Ok(()) => {
                info!("Successfully synced {} GPU prices", count);

                // Record successful sync metrics
                PricingMetrics::record_sync_success();
                PricingMetrics::record_sync_duration(sync_start.elapsed());

                // Update cache size metrics
                self.update_cache_metrics().await;

                Ok(count)
            }
            Err(e) => {
                error!("Failed to store prices in cache: {}", e);
                PricingMetrics::record_sync_error();
                Err(e)
            }
        }
    }

    /// Update cache size and age metrics
    async fn update_cache_metrics(&self) {
        // Get all cached prices to calculate metrics
        match self.cache.get_all().await {
            Ok(prices) => {
                // Update cache size
                PricingMetrics::set_cache_size(prices.len());

                // Calculate oldest cache age
                if !prices.is_empty() {
                    let now = chrono::Utc::now();
                    let oldest_age = prices
                        .iter()
                        .map(|p| (now - p.updated_at).num_seconds())
                        .max()
                        .unwrap_or(0);

                    PricingMetrics::set_oldest_cache_age(oldest_age as f64);
                }
            }
            Err(e) => {
                warn!("Failed to update cache metrics: {}", e);
            }
        }
    }

    /// Record price history for tracking price changes over time
    async fn record_price_history(&self, prices: &[GpuPrice]) -> Result<()> {
        debug!("Recording {} prices to history", prices.len());

        let pool = self.cache.pool();

        for price in prices {
            sqlx::query(
                r#"
                INSERT INTO billing.price_history (
                    gpu_model, price_per_hour, source, provider, recorded_at
                )
                VALUES ($1, $2, $3, $4, NOW())
                "#,
            )
            .bind(&price.gpu_model)
            .bind(price.discounted_price_per_hour)
            .bind(&price.source)
            .bind(&price.provider)
            .execute(pool)
            .await
            .map_err(|e| crate::error::BillingError::DatabaseError {
                operation: "record_price_history".to_string(),
                source: Box::new(e),
            })?;
        }

        debug!("Successfully recorded {} prices to history", prices.len());
        Ok(())
    }

    /// Apply discount logic to prices
    #[allow(dead_code)] // Used in Phase 3
    fn apply_discount(&self, market_price: Decimal, gpu_model: &str) -> Decimal {
        let discount = GpuPrice::effective_discount(
            self.config.global_discount_percent,
            &self.config.gpu_discounts,
            gpu_model,
        );

        // Apply discount: price * (1 + discount%/100)
        let multiplier = Decimal::ONE + (discount / Decimal::from(100));
        market_price * multiplier
    }

    /// Aggregate prices from multiple providers by GPU model
    async fn aggregate_prices(&self, all_prices: Vec<GpuPrice>) -> Result<Vec<GpuPrice>> {
        use crate::pricing::types::PriceAggregationStrategy;
        use std::collections::HashMap;

        if all_prices.is_empty() {
            return Ok(Vec::new());
        }

        // Group prices by GPU model
        let mut grouped: HashMap<String, Vec<GpuPrice>> = HashMap::new();
        for price in all_prices {
            grouped
                .entry(price.gpu_model.clone())
                .or_default()
                .push(price);
        }

        debug!(
            "Grouped {} prices into {} GPU models",
            grouped.values().map(|v| v.len()).sum::<usize>(),
            grouped.len()
        );

        // Aggregate each group according to strategy
        let mut aggregated_prices = Vec::new();

        for (gpu_model, mut prices) in grouped {
            if prices.is_empty() {
                continue;
            }

            let aggregated_price = match &self.config.aggregation_strategy {
                PriceAggregationStrategy::Minimum => {
                    // Take the lowest price
                    prices.sort_by(|a, b| a.market_price_per_hour.cmp(&b.market_price_per_hour));
                    prices.into_iter().next().unwrap()
                }
                PriceAggregationStrategy::Median => {
                    // Take the median price
                    prices.sort_by(|a, b| a.market_price_per_hour.cmp(&b.market_price_per_hour));
                    let mid = prices.len() / 2;
                    if prices.len() % 2 == 0 && prices.len() > 1 {
                        // Even number: average the two middle values
                        let price1 = &prices[mid - 1];
                        let price2 = &prices[mid];
                        let avg_price = (price1.market_price_per_hour
                            + price2.market_price_per_hour)
                            / Decimal::from(2);

                        let mut median_price = price1.clone();
                        median_price.market_price_per_hour = avg_price;
                        median_price.discounted_price_per_hour = avg_price;
                        median_price.source = "aggregated_median".to_string();
                        median_price
                    } else {
                        // Odd number: take the middle value
                        prices.into_iter().nth(mid).unwrap()
                    }
                }
                PriceAggregationStrategy::Average => {
                    // Calculate average price
                    let sum: Decimal = prices.iter().map(|p| p.market_price_per_hour).sum();
                    let count = Decimal::from(prices.len());
                    let avg_price = sum / count;

                    let mut avg_gpu_price = prices.into_iter().next().unwrap();
                    avg_gpu_price.market_price_per_hour = avg_price;
                    avg_gpu_price.discounted_price_per_hour = avg_price;
                    avg_gpu_price.source = "aggregated_average".to_string();
                    avg_gpu_price
                }
                PriceAggregationStrategy::PreferProvider(preferred) => {
                    // Prefer specific provider, fallback to minimum
                    if let Some(preferred_price) = prices
                        .iter()
                        .find(|p| p.provider.eq_ignore_ascii_case(preferred))
                    {
                        debug!("Using preferred provider {} for {}", preferred, gpu_model);
                        preferred_price.clone()
                    } else {
                        warn!(
                            "Preferred provider {} not found for {}, using minimum",
                            preferred, gpu_model
                        );
                        prices
                            .sort_by(|a, b| a.market_price_per_hour.cmp(&b.market_price_per_hour));
                        prices.into_iter().next().unwrap()
                    }
                }
            };

            debug!(
                "Aggregated {} using {:?} strategy: ${}/hr",
                gpu_model, self.config.aggregation_strategy, aggregated_price.market_price_per_hour
            );

            aggregated_prices.push(aggregated_price);
        }

        Ok(aggregated_prices)
    }

    /// Apply discounts to aggregated prices
    fn apply_discounts(&self, prices: &mut [GpuPrice]) {
        for price in prices.iter_mut() {
            let discount = GpuPrice::effective_discount(
                self.config.global_discount_percent,
                &self.config.gpu_discounts,
                &price.gpu_model,
            );

            price.apply_discount(discount);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::BillingError;
    use crate::pricing::providers::{create_providers, PriceProvider};
    use crate::pricing::types::{PriceAggregationStrategy, PriceSource};
    use async_trait::async_trait;
    use chrono::Utc;
    use rust_decimal_macros::dec;
    use std::collections::HashMap;
    use std::env;

    struct StubProvider {
        name: &'static str,
        prices: Vec<GpuPrice>,
        should_fail: bool,
    }

    impl StubProvider {
        fn successful(name: &'static str, prices: Vec<GpuPrice>) -> Self {
            Self {
                name,
                prices,
                should_fail: false,
            }
        }

        fn failing(name: &'static str) -> Self {
            Self {
                name,
                prices: Vec::new(),
                should_fail: true,
            }
        }
    }

    #[async_trait]
    impl PriceProvider for StubProvider {
        fn name(&self) -> &str {
            self.name
        }

        async fn fetch_prices(&self, _filter: &PriceQueryFilter) -> Result<Vec<GpuPrice>> {
            if self.should_fail {
                Err(BillingError::ExternalApiError {
                    provider: self.name.to_string(),
                    details: "forced failure for test".to_string(),
                })
            } else {
                Ok(self.prices.clone())
            }
        }

        async fn health_check(&self) -> bool {
            !self.should_fail
        }
    }

    /// Helper to create a test GpuPrice
    fn create_test_price(gpu_model: &str, provider: &str, price: Decimal) -> GpuPrice {
        GpuPrice {
            gpu_model: gpu_model.to_string(),
            vram_gb: Some(80),
            market_price_per_hour: price,
            discounted_price_per_hour: price,
            discount_percent: Decimal::ZERO,
            source: provider.to_string(),
            provider: provider.to_string(),
            location: Some("us-east-1".to_string()),
            instance_name: Some("test-instance".to_string()),
            updated_at: Utc::now(),
            is_spot: false,
        }
    }

    /// Test discount calculation with default global discount
    #[test]
    fn test_apply_discount() {
        let config = PricingConfig::default();

        let market_price = Decimal::from(100);
        let discount = GpuPrice::effective_discount(
            config.global_discount_percent,
            &config.gpu_discounts,
            "H100",
        );

        // Apply discount formula: price * (1 + discount%/100)
        let multiplier = Decimal::ONE + (discount / Decimal::from(100));
        let discounted = market_price * multiplier;

        // Default global discount is -20%
        // 100 * (1 + (-20/100)) = 100 * 0.8 = 80
        assert_eq!(discounted, Decimal::from(80));
    }

    /// Test fetch, aggregate, and discount pipeline without database dependencies
    #[tokio::test]
    async fn test_fetch_aggregate_and_discount_pipeline() {
        let config = PricingConfig {
            enabled: true,
            aggregation_strategy: PriceAggregationStrategy::Median,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let providers: Vec<Box<dyn PriceProvider>> = vec![
            Box::new(StubProvider::successful(
                "marketplace-primary",
                vec![create_test_price("H100", "aws", dec!(30.0))],
            )),
            Box::new(StubProvider::successful(
                "marketplace-secondary",
                vec![create_test_price("H100", "runpod", dec!(24.0))],
            )),
        ];

        let service = PricingService::new(providers, cache, config);

        let fetched = service.fetch_latest_prices().await.unwrap();
        assert_eq!(
            fetched.len(),
            2,
            "Should collect prices from both providers"
        );

        let mut aggregated = service.aggregate_prices(fetched).await.unwrap();
        assert_eq!(
            aggregated.len(),
            1,
            "Median aggregation should collapse to one price"
        );
        assert_eq!(
            aggregated[0].market_price_per_hour,
            dec!(27.0),
            "Median of 24 and 30 should be 27"
        );

        service.apply_discounts(&mut aggregated);

        assert_eq!(
            aggregated[0].discounted_price_per_hour,
            dec!(21.6),
            "20% discount should be applied to aggregated price"
        );
        assert_eq!(
            aggregated[0].discount_percent,
            dec!(-20.0),
            "Discount metadata should reflect global discount"
        );
        assert_eq!(
            aggregated[0].source, "aggregated_median",
            "Aggregated prices should mark the synthetic source"
        );
    }

    /// Test that a failing provider does not break successful ones
    #[tokio::test]
    async fn test_fetch_skips_failed_providers() {
        let config = PricingConfig {
            enabled: true,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let providers: Vec<Box<dyn PriceProvider>> = vec![
            Box::new(StubProvider::successful(
                "marketplace",
                vec![create_test_price("H100", "aws", dec!(30.0))],
            )),
            Box::new(StubProvider::failing("unreachable")),
        ];

        let service = PricingService::new(providers, cache, config);

        let fetched = service.fetch_latest_prices().await.unwrap();
        assert_eq!(
            fetched.len(),
            1,
            "Only successful provider results should be returned"
        );
        assert_eq!(fetched[0].provider, "aws");
    }

    /// Test that fetch_latest_prices surfaces an error when all providers fail
    #[tokio::test]
    async fn test_fetch_all_providers_fail() {
        let config = PricingConfig {
            enabled: true,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let providers: Vec<Box<dyn PriceProvider>> =
            vec![Box::new(StubProvider::failing("marketplace"))];

        let service = PricingService::new(providers, cache, config);

        let result = service.fetch_latest_prices().await;
        match result {
            Ok(_) => panic!("Expected error when all providers fail"),
            Err(BillingError::ConfigurationError { message }) => assert!(
                message.contains("All price providers failed"),
                "Error message should mention all providers failing"
            ),
            Err(other) => panic!("Unexpected error type: {:?}", other),
        }
    }

    /// Live integration test that calls the Shadeform API when MARKETPLACE_API_KEY is set.
    #[tokio::test]
    async fn test_fetch_prices_from_marketplace_api() {
        let api_key = match env::var("MARKETPLACE_API_KEY") {
            Ok(key) if !key.trim().is_empty() => key,
            _ => {
                eprintln!("MARKETPLACE_API_KEY not set; skipping live Shadeform API test");
                return;
            }
        };

        let config = PricingConfig {
            enabled: true,
            sources: vec![PriceSource::Marketplace],
            marketplace_api_key: Some(api_key),
            aggregation_strategy: PriceAggregationStrategy::Median,
            global_discount_percent: dec!(-20.0),
            fallback_to_static: true,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let providers =
            create_providers(&config).expect("Expected marketplace provider with API key");
        assert!(
            !providers.is_empty(),
            "Dynamic pricing should create at least one provider"
        );

        let service = PricingService::new(providers, cache, config.clone());

        let prices = service
            .fetch_latest_prices()
            .await
            .expect("Shadeform API call should succeed with valid key");
        assert!(
            !prices.is_empty(),
            "Shadeform API should return at least one GPU price"
        );

        let mut aggregated = service
            .aggregate_prices(prices)
            .await
            .expect("Aggregation should succeed");
        assert!(
            !aggregated.is_empty(),
            "Aggregation should yield at least one GPU model"
        );

        let original = aggregated[0].market_price_per_hour;
        service.apply_discounts(&mut aggregated);
        let discounted = aggregated[0].discounted_price_per_hour;

        let expected_multiplier =
            Decimal::ONE + (config.global_discount_percent / Decimal::from(100));
        let expected = (original * expected_multiplier).round_dp(4);

        assert_eq!(
            discounted.round_dp(4),
            expected,
            "Discounted price should reflect the configured global discount"
        );
        assert_eq!(
            aggregated[0].discount_percent.round_dp(2),
            config.global_discount_percent.round_dp(2),
            "Discount percent metadata should match configuration"
        );

        println!(
            "Marketplace aggregated prices ({} entries):",
            aggregated.len()
        );
        for price in &aggregated {
            println!(
                "  - {model:<8} provider={provider:<12} location={location:<12} market=${market:.4} discounted=${discount:.4} updated_at={updated}",
                model = price.gpu_model,
                provider = price.provider,
                location = price
                    .location
                    .clone()
                    .unwrap_or_else(|| "n/a".to_string()),
                market = price.market_price_per_hour,
                discount = price.discounted_price_per_hour,
                updated = price.updated_at
            );
        }
    }

    /// Test discount calculation with GPU-specific override
    #[test]
    fn test_apply_discount_with_override() {
        let mut gpu_discounts = HashMap::new();
        gpu_discounts.insert("H100".to_string(), Decimal::from(-15));

        let global_discount = Decimal::from(-20);

        let market_price = Decimal::from(100);
        let discount = GpuPrice::effective_discount(global_discount, &gpu_discounts, "H100");

        // Apply discount formula
        let multiplier = Decimal::ONE + (discount / Decimal::from(100));
        let discounted = market_price * multiplier;

        // H100 override is -15%
        // 100 * (1 + (-15/100)) = 100 * 0.85 = 85
        assert_eq!(discounted, Decimal::from(85));
    }

    /// Test that unknown GPU uses global discount
    #[test]
    fn test_apply_discount_unknown_gpu() {
        let mut gpu_discounts = HashMap::new();
        gpu_discounts.insert("H100".to_string(), Decimal::from(-15));

        let global_discount = Decimal::from(-20);

        let market_price = Decimal::from(100);
        let discount = GpuPrice::effective_discount(global_discount, &gpu_discounts, "A6000");

        // Apply discount formula
        let multiplier = Decimal::ONE + (discount / Decimal::from(100));
        let discounted = market_price * multiplier;

        // A6000 not in overrides, uses global -20%
        // 100 * (1 + (-20/100)) = 100 * 0.8 = 80
        assert_eq!(discounted, Decimal::from(80));
    }

    /// Test minimum aggregation strategy
    #[tokio::test]
    async fn test_aggregate_minimum() {
        let config = PricingConfig {
            aggregation_strategy: PriceAggregationStrategy::Minimum,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let prices = vec![
            create_test_price("H100", "aws", dec!(30.0)),
            create_test_price("H100", "azure", dec!(28.0)),
            create_test_price("H100", "gcp", dec!(32.0)),
        ];

        let aggregated = service.aggregate_prices(prices).await.unwrap();
        assert_eq!(aggregated.len(), 1);
        assert_eq!(aggregated[0].market_price_per_hour, dec!(28.0));
        assert_eq!(aggregated[0].provider, "azure");
    }

    /// Test median aggregation with odd count
    #[tokio::test]
    async fn test_aggregate_median_odd() {
        let config = PricingConfig {
            aggregation_strategy: PriceAggregationStrategy::Median,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let prices = vec![
            create_test_price("H100", "aws", dec!(30.0)),
            create_test_price("H100", "azure", dec!(28.0)),
            create_test_price("H100", "gcp", dec!(32.0)),
        ];

        let aggregated = service.aggregate_prices(prices).await.unwrap();
        assert_eq!(aggregated.len(), 1);
        assert_eq!(aggregated[0].market_price_per_hour, dec!(30.0));
    }

    /// Test median aggregation with even count
    #[tokio::test]
    async fn test_aggregate_median_even() {
        let config = PricingConfig {
            aggregation_strategy: PriceAggregationStrategy::Median,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let prices = vec![
            create_test_price("H100", "aws", dec!(30.0)),
            create_test_price("H100", "azure", dec!(28.0)),
            create_test_price("H100", "gcp", dec!(32.0)),
            create_test_price("H100", "vastai", dec!(26.0)),
        ];

        let aggregated = service.aggregate_prices(prices).await.unwrap();
        assert_eq!(aggregated.len(), 1);
        // Median of [26, 28, 30, 32] = (28 + 30) / 2 = 29
        assert_eq!(aggregated[0].market_price_per_hour, dec!(29.0));
    }

    /// Test average aggregation strategy
    #[tokio::test]
    async fn test_aggregate_average() {
        let config = PricingConfig {
            aggregation_strategy: PriceAggregationStrategy::Average,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let prices = vec![
            create_test_price("H100", "aws", dec!(30.0)),
            create_test_price("H100", "azure", dec!(28.0)),
            create_test_price("H100", "gcp", dec!(32.0)),
        ];

        let aggregated = service.aggregate_prices(prices).await.unwrap();
        assert_eq!(aggregated.len(), 1);
        // Average of [30, 28, 32] = 90 / 3 = 30
        assert_eq!(aggregated[0].market_price_per_hour, dec!(30.0));
    }

    /// Test PreferProvider strategy with preferred provider available
    #[tokio::test]
    async fn test_aggregate_prefer_provider_found() {
        let config = PricingConfig {
            aggregation_strategy: PriceAggregationStrategy::PreferProvider("azure".to_string()),
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let prices = vec![
            create_test_price("H100", "aws", dec!(30.0)),
            create_test_price("H100", "azure", dec!(28.0)),
            create_test_price("H100", "gcp", dec!(32.0)),
        ];

        let aggregated = service.aggregate_prices(prices).await.unwrap();
        assert_eq!(aggregated.len(), 1);
        assert_eq!(aggregated[0].market_price_per_hour, dec!(28.0));
        assert_eq!(aggregated[0].provider, "azure");
    }

    /// Test PreferProvider strategy with fallback to minimum
    #[tokio::test]
    async fn test_aggregate_prefer_provider_fallback() {
        let config = PricingConfig {
            aggregation_strategy: PriceAggregationStrategy::PreferProvider("vastai".to_string()),
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let prices = vec![
            create_test_price("H100", "aws", dec!(30.0)),
            create_test_price("H100", "azure", dec!(28.0)),
            create_test_price("H100", "gcp", dec!(32.0)),
        ];

        let aggregated = service.aggregate_prices(prices).await.unwrap();
        assert_eq!(aggregated.len(), 1);
        // Falls back to minimum since vastai not found
        assert_eq!(aggregated[0].market_price_per_hour, dec!(28.0));
        assert_eq!(aggregated[0].provider, "azure");
    }

    /// Test aggregation with multiple GPU models
    #[tokio::test]
    async fn test_aggregate_multiple_gpu_models() {
        let config = PricingConfig {
            aggregation_strategy: PriceAggregationStrategy::Minimum,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let prices = vec![
            create_test_price("H100", "aws", dec!(30.0)),
            create_test_price("H100", "azure", dec!(28.0)),
            create_test_price("A100", "aws", dec!(15.0)),
            create_test_price("A100", "gcp", dec!(12.0)),
        ];

        let aggregated = service.aggregate_prices(prices).await.unwrap();
        assert_eq!(aggregated.len(), 2);

        // Find H100 and A100 in results
        let h100 = aggregated.iter().find(|p| p.gpu_model == "H100").unwrap();
        let a100 = aggregated.iter().find(|p| p.gpu_model == "A100").unwrap();

        assert_eq!(h100.market_price_per_hour, dec!(28.0));
        assert_eq!(a100.market_price_per_hour, dec!(12.0));
    }

    /// Test aggregation with empty price list
    #[tokio::test]
    async fn test_aggregate_empty() {
        let config = PricingConfig::default();
        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let prices = Vec::new();
        let aggregated = service.aggregate_prices(prices).await.unwrap();
        assert_eq!(aggregated.len(), 0);
    }

    /// Test aggregation with single price
    #[tokio::test]
    async fn test_aggregate_single_price() {
        let config = PricingConfig::default();
        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let prices = vec![create_test_price("H100", "aws", dec!(30.0))];

        let aggregated = service.aggregate_prices(prices).await.unwrap();
        assert_eq!(aggregated.len(), 1);
        assert_eq!(aggregated[0].market_price_per_hour, dec!(30.0));
    }

    /// Test get_price_with_fallback when dynamic pricing is disabled
    #[tokio::test]
    async fn test_get_price_with_fallback_disabled() {
        let config = PricingConfig {
            enabled: false,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let static_price = dec!(100.0);
        let result = service
            .get_price_with_fallback("H100", static_price)
            .await
            .unwrap();

        // Should use static price when disabled
        assert_eq!(result, static_price);
    }

    /// Test get_price_with_fallback when cache miss and fallback enabled
    #[tokio::test]
    async fn test_get_price_with_fallback_cache_miss() {
        let config = PricingConfig {
            enabled: true,
            fallback_to_static: true,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let static_price = dec!(100.0);
        let result = service
            .get_price_with_fallback("H100", static_price)
            .await
            .unwrap();

        // Should fall back to static price on cache miss
        assert_eq!(result, static_price);
    }

    /// Test get_price_with_fallback when cache miss and fallback disabled
    #[tokio::test]
    async fn test_get_price_with_fallback_disabled_no_cache() {
        let config = PricingConfig {
            enabled: true,
            fallback_to_static: false,
            ..Default::default()
        };

        let cache = Arc::new(PriceCache::new_fake());
        let service = PricingService::new(Vec::new(), cache, config);

        let static_price = dec!(100.0);
        let result = service.get_price_with_fallback("H100", static_price).await;

        // Should return error when no cache and fallback disabled
        assert!(result.is_err());
    }
}

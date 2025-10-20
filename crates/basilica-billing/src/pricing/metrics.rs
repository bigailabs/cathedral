use metrics::{counter, gauge, histogram};
use std::time::Instant;

/// Pricing metrics for monitoring GPU price fetching and sync operations
pub struct PricingMetrics;

impl PricingMetrics {
    /// Increment the pricing sync counter (successful syncs)
    pub fn record_sync_success() {
        counter!("basilca_billing_pricing_sync_total").increment(1);
    }

    /// Increment the pricing sync errors counter
    pub fn record_sync_error() {
        counter!("basilca_billing_pricing_sync_errors_total").increment(1);
    }

    /// Record the duration of a pricing fetch operation for a specific provider
    pub fn record_fetch_duration(provider: &str, duration: std::time::Duration) {
        histogram!(
            "basilca_billing_pricing_fetch_duration_seconds",
            &[("provider", provider.to_string())]
        )
        .record(duration.as_secs_f64());
    }

    /// Update the cache size gauge
    pub fn set_cache_size(size: usize) {
        gauge!("basilca_billing_pricing_cache_size").set(size as f64);
    }

    /// Update the oldest cache age gauge (in seconds)
    pub fn set_oldest_cache_age(age_seconds: f64) {
        gauge!("basilca_billing_pricing_oldest_cache_age_seconds").set(age_seconds);
    }

    /// Increment the fallback to static pricing counter
    pub fn record_fallback_to_static(gpu_model: &str) {
        counter!(
            "basilca_billing_pricing_fallback_to_static_total",
            &[("gpu_model", gpu_model.to_string())]
        )
        .increment(1);
    }

    /// Create a timer for measuring price sync operations
    pub fn start_sync_timer() -> Instant {
        Instant::now()
    }

    /// Record the complete sync duration
    pub fn record_sync_duration(duration: std::time::Duration) {
        histogram!("basilca_billing_pricing_sync_duration_seconds").record(duration.as_secs_f64());
    }

    /// Record the number of prices fetched from a provider
    pub fn record_prices_fetched(provider: &str, count: usize) {
        counter!(
            "basilca_billing_pricing_prices_fetched_total",
            &[("provider", provider.to_string())]
        )
        .increment(count as u64);
    }

    /// Record a provider fetch error
    pub fn record_provider_error(provider: &str) {
        counter!(
            "basilca_billing_pricing_provider_errors_total",
            &[("provider", provider.to_string())]
        )
        .increment(1);
    }

    /// Record cache hits
    pub fn record_cache_hit(gpu_model: &str) {
        counter!(
            "basilca_billing_pricing_cache_hits_total",
            &[("gpu_model", gpu_model.to_string())]
        )
        .increment(1);
    }

    /// Record cache misses
    pub fn record_cache_miss(gpu_model: &str) {
        counter!(
            "basilca_billing_pricing_cache_misses_total",
            &[("gpu_model", gpu_model.to_string())]
        )
        .increment(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pricing_metrics_instantiation() {
        // Just ensure the metrics can be called without panicking
        PricingMetrics::record_sync_success();
        PricingMetrics::record_sync_error();
        PricingMetrics::set_cache_size(10);
        PricingMetrics::set_oldest_cache_age(3600.0);
        PricingMetrics::record_fallback_to_static("H100");
    }

    #[test]
    fn test_timing_metrics() {
        let start = PricingMetrics::start_sync_timer();
        std::thread::sleep(std::time::Duration::from_millis(10));
        let duration = start.elapsed();
        PricingMetrics::record_sync_duration(duration);
        assert!(duration.as_millis() >= 10);
    }

    #[test]
    fn test_provider_metrics() {
        PricingMetrics::record_prices_fetched("VastAI", 5);
        PricingMetrics::record_provider_error("AWS");
        PricingMetrics::record_fetch_duration("Azure", std::time::Duration::from_secs(2));
    }
}
